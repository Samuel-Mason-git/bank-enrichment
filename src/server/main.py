from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, HTTPException, Depends, Request, status, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional
from urllib.parse import urlsplit
import asyncio
import uvicorn
import html
import logging
import re
import secrets
import time
import json
import os

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from server_db import (
    init_db, get_con, get_quick_categories, now, payload, record_context, record_label, record_request,
    record_update, subcategory, subcategory_id,
)
from telegram import TelegramBot
from check_rules import check_rules, NO_MATCH
from follow_up_tg import requester_loop
from monzo_oauth import (
    begin_authorization, complete_authorization, fetch_transaction, monzo_refresh_loop, MONZO_CLIENT_ID,
)

BASE_DIR = os.path.dirname(__file__)

LOG_PATH = os.path.join(BASE_DIR, "..", "..", "data", "server.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=5),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


class _DropOauthCallbackAccessLogs(logging.Filter):
    """Uvicorn's access logger writes the full request line, query string
    included, for every request -- unlike every other route here, /oauth/
    callback's query string carries a (single-use, but still sensitive while
    valid) authorization code. Easier and more robust to drop that one
    route's access-log line entirely than to try to reconstruct and redact it
    from uvicorn's internal log-record args, whose order isn't a public API."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.args and any("/oauth/callback" in str(a) for a in record.args))


class _RedactWebhookTokens(logging.Filter):
    """The Monzo webhook secret travels in its URL (Monzo can't send custom
    headers), and uvicorn logs every request line in full -- so the secret
    would otherwise sit in plain text in the container logs. Redacts the
    value wherever it appears rather than dropping the line, because those
    lines are the main way to see that webhooks are actually arriving."""

    _token = re.compile(r"(\btoken=)[^&\s\"]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and isinstance(record.args, tuple):
            record.args = tuple(
                self._token.sub(r"\1[redacted]", a) if isinstance(a, str) else a for a in record.args
            )
        return True


logging.getLogger("uvicorn.access").addFilter(_DropOauthCallbackAccessLogs())
logging.getLogger("uvicorn.access").addFilter(_RedactWebhookTokens())

# Shared secrets proving an inbound webhook really came from Telegram/Monzo --
# both endpoints are otherwise open to anyone who knows the URL. Optional so a
# deploy never breaks an install that hasn't set them yet (deploys happen
# automatically on merge), but startup warns loudly until both are set.
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
MONZO_WEBHOOK_SECRET = os.getenv("MONZO_WEBHOOK_SECRET")


def _secret_matches(expected: str | None, supplied: str | None) -> bool:
    """True if no secret is configured, or the supplied one matches it.
    Constant-time, so response timing can't leak the secret a byte at a time."""
    if not expected:
        return True
    return bool(supplied) and secrets.compare_digest(supplied.encode(), expected.encode())


def _unset_webhook_secrets() -> list[str]:
    return [name for name, value in (("TELEGRAM_WEBHOOK_SECRET", TELEGRAM_WEBHOOK_SECRET),
                                     ("MONZO_WEBHOOK_SECRET", MONZO_WEBHOOK_SECRET)) if not value]


def _is_same_site(request: Request) -> bool:
    """Whether a browser request came from a page on this server.

    The dashboard uses HTTP Basic auth, which browsers attach automatically --
    even to a form another website submits -- so without this, any page you
    visited while logged in could create, edit or delete rules. Browsers
    always send Origin on a cross-site POST, so a mismatched (or "null")
    Origin is rejected. A request with neither Origin nor Referer isn't a
    browser being tricked into anything (curl, a script) and is let through."""
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    if origin is not None:
        return urlsplit(origin).netloc == host
    referer = request.headers.get("referer")
    if referer is not None:
        return urlsplit(referer).netloc == host
    return True

DASHBOARD_USER = os.getenv("DASHBOARD_USER")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
_sessions: dict = {}
_pending_skips: dict = {}

security = HTTPBasic()
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))
templates.env.globals["static_version"] = str(int(time.time()))


def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    if not DASHBOARD_USER or not DASHBOARD_PASSWORD:
        raise HTTPException(status_code=503, detail="Dashboard credentials not configured")
    correct_user = secrets.compare_digest(credentials.username, DASHBOARD_USER)
    correct_pass = secrets.compare_digest(credentials.password, DASHBOARD_PASSWORD)
    if not (correct_user and correct_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

bot: TelegramBot | None = None

SESSION_TIMEOUT = 180  # seconds


async def _enrich_timeout(chat_id: int, transaction_id: str):
    await asyncio.sleep(SESSION_TIMEOUT)
    session = _sessions.get(chat_id)
    if session and session.get("transaction_id") == transaction_id:
        del _sessions[chat_id]
        if bot:
            bot.send_message(chat_id, "⏱ Enrich session expired. Tap ✏️ Enrich again when ready.")


async def _skip_timeout(chat_id: int, message_id: int):
    await asyncio.sleep(SESSION_TIMEOUT)
    _pending_skips.pop(chat_id, None)
    if bot and message_id:
        bot.delete_message(chat_id, message_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot
    init_db()
    log.info("Database initialised")
    try:
        bot = TelegramBot()
        log.info("Telegram bot initialised")
    except ValueError as e:
        log.warning(f"Telegram bot not initialised: {e}")
    unset = _unset_webhook_secrets()
    if unset:
        log.warning(
            f"{' and '.join(unset)} not set -- the matching webhook endpoint accepts requests from "
            "anyone who knows its URL. See SETUP.md steps 4, 8 and 9."
        )
        if bot and os.getenv("TELEGRAM_CHAT_ID"):
            try:
                bot.send_alert(
                    int(os.getenv("TELEGRAM_CHAT_ID")), "Webhooks aren't secured yet",
                    f"{' and '.join(unset)} not set, so anyone who knows your server's URL can send it "
                    "fake transactions or fake button taps. See SETUP.md steps 4, 8 and 9.",
                )
            except Exception as e:
                log.warning(f"Could not send the webhook-secret alert: {e}")
    task = asyncio.create_task(requester_loop(bot))
    log.info("Requester loop started")
    monzo_task = asyncio.create_task(monzo_refresh_loop(bot))
    log.info("Monzo OAuth refresh loop started")
    yield
    task.cancel()
    monzo_task.cancel()
    for t in (task, monzo_task):
        try:
            await t
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.get("/health")
def health():
    """For an uptime monitor: 200 while the server is up and can reach the
    database, 503 when it can't. Says nothing else, so it needs no login."""
    try:
        get_con().execute("SELECT 1")
    except Exception as e:
        log.error(f"Health check: database unreachable: {e}")
        raise HTTPException(status_code=503, detail="database unreachable")
    return {"ok": True}


@app.middleware("http")
async def reject_cross_site_dashboard_posts(request: Request, call_next):
    if request.method == "POST" and request.url.path.startswith("/dashboard") and not _is_same_site(request):
        log.warning(f"Blocked a cross-site POST to {request.url.path} (Origin: {request.headers.get('origin')!r})")
        return PlainTextResponse("Cross-site request blocked.", status_code=403)
    return await call_next(request)


def _public_base_url(request: Request) -> str:
    """https://your-domain, as the outside world sees it. Behind Caddy the app
    itself only ever sees plain http, so the scheme Caddy forwarded wins."""
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{scheme}://{request.headers.get('host', request.url.netloc)}"


@app.get("/oauth/start")
async def oauth_start(request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    """Begin (or redo) Monzo authorization: sends you to Monzo's login with a
    fresh single-use state. Behind the dashboard login, so only you can
    start a flow whose result gets stored."""
    if not MONZO_CLIENT_ID:
        return HTMLResponse("<p>MONZO_CLIENT_ID isn't set on the server -- see SETUP.md.</p>", status_code=503)
    return RedirectResponse(begin_authorization(f"{_public_base_url(request)}/oauth/callback"), status_code=303)


@app.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(code: str = "", state: str = "", error: str = ""):
    """Monzo's OAuth redirect lands here, so it can't sit behind the dashboard
    login -- Monzo's redirect doesn't carry your credentials. What protects
    it instead is the state: only a flow started from /oauth/start (which IS
    behind the login) is ever completed. The exchange happens entirely
    server-side; neither the code nor any token is rendered or logged."""
    if error:
        log.error(f"Monzo OAuth authorization failed: {error}")
        return "<p>Authorization failed. Check the server logs.</p>"
    if not code or not state:
        return "<p>No authorization code received. Start from /oauth/start.</p>"
    if complete_authorization(code, state):
        return "<p>Monzo authorization complete. Tokens stored -- you can close this tab.</p>"
    return "<p>That authorization link has expired or wasn't started here. Start again from /oauth/start.</p>"


@app.get("/dashboard/logout", response_class=HTMLResponse)
async def logout(request: Request):
    """HTTP Basic auth has no real server-side session to end, and forcing a
    401 here would trap the browser in a prompt loop it can never satisfy
    (this endpoint would keep rejecting every credential re-entered). Instead,
    just explain the limitation plainly."""
    return templates.TemplateResponse(request=request, name="logout.html")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    log.error(f"422 validation error. Body: {body.decode()}")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


class MonzoInner(BaseModel):
    model_config = ConfigDict(extra='ignore')
    account_id: str
    amount: int
    created: str
    currency: str
    description: str
    id: str
    category: str
    is_load: bool
    settled: Optional[str] = None
    merchant: Optional[dict] = None
    counterparty: Optional[dict] = None
    # Present only when the payment was refused (e.g. 'INSUFFICIENT_FUNDS').
    # Monzo still fires a webhook for these even though no money moved.
    decline_reason: Optional[str] = None
    # True while the amount is a provisional hold, not the final charge -- the
    # classic UK fuel-pump pattern, where the pump authorises a flat ~£99/£100
    # before it knows how much fuel was actually taken. The real amount arrives
    # as its OWN separate transaction (its own id), not a correction to this
    # one, so there is nothing to reconcile here -- only to not alarm the user
    # with a number that was never going to be the real charge.
    amount_is_pending: Optional[bool] = None
    # Monzo's own payment scheme -- 'uk_retail_pot' for a pot transfer, and
    # distinct values for card payments, direct debits and faster payments.
    # A rule keyed on this cannot be fooled by a merchant whose name happens to
    # contain the same characters as the pattern being matched.
    scheme: Optional[str] = None

class MonzoOuter(BaseModel):
    model_config = ConfigDict(extra='ignore')
    type: str
    data: MonzoInner

class TelegramUser(BaseModel):
    id: int
    first_name: str
    is_bot: bool
    last_name: Optional[str] = None
    username: Optional[str] = None

class TelegramChat(BaseModel):
    id: int
    type: str
    title: Optional[str] = None
    username: Optional[str] = None
    first_name: Optional[str] = None

class TelegramMessage(BaseModel):
    model_config = ConfigDict(extra='ignore', populate_by_name=True)
    message_id: int
    chat: TelegramChat
    date: int
    from_user: Optional[TelegramUser] = Field(None, alias="from")
    text: Optional[str] = None

class TelegramCallbackQuery(BaseModel):
    model_config = ConfigDict(extra='ignore', populate_by_name=True)
    id: str
    from_user: TelegramUser = Field(alias="from")
    message: Optional[TelegramMessage] = None
    data: Optional[str] = None

class TelegramUpdate(BaseModel):
    model_config = ConfigDict(extra='ignore')
    update_id: int
    message: Optional[TelegramMessage] = None
    edited_message: Optional[TelegramMessage] = None
    callback_query: Optional[TelegramCallbackQuery] = None

@app.post('/recieve_monzo/')
async def recieve_monzo(request: Request):
    # Monzo doesn't sign webhooks, so the only proof one came from Monzo is the
    # secret token registered in its URL. Checked before anything is parsed
    # or logged, so a forged request leaves no trace beyond the access line.
    if not _secret_matches(MONZO_WEBHOOK_SECRET, request.query_params.get("token")):
        log.warning("Rejected a Monzo webhook with a missing or wrong token")
        raise HTTPException(status_code=403, detail="Forbidden")
    body = await request.body()
    try:
        monzo_data = MonzoOuter.model_validate_json(body)
    except Exception as e:
        log.error(f"Validation error: {e}. Body: {body.decode()}")
        raise HTTPException(status_code=422, detail="Invalid payload")

    transaction_id = monzo_data.data.id
    con = get_con()

    # Store it. Monzo sends several webhooks per payment: the first is the
    # transaction, and any later one that says something new (settled, a
    # merchant filled in, a final amount) is kept as an update. Only the first
    # asks for context.
    try:
        is_new = con.execute(
            """INSERT INTO transactions_raw (id, payload, received_at) VALUES (%s, %s, %s)
               ON CONFLICT (id) DO NOTHING RETURNING id""",
            [transaction_id, body.decode(), now()],
        ).fetchone() is not None
        if is_new:
            log.info(f"Transaction stored: {transaction_id}")
        elif record_update(con, transaction_id, json.loads(body), "webhook"):
            log.info(f"Update stored for {transaction_id} ({monzo_data.type})")
        else:
            log.info(f"Duplicate webhook ignored: {transaction_id}")
    except Exception as e:
        log.error(f"Failed to store transaction {transaction_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to store transaction")
    if not is_new:
        return {"status": "ok"}

    # A declined payment moved no money, but Monzo fires a webhook for it just
    # like a real one -- and the retry that succeeds arrives seconds later as a
    # separate transaction. Left alone it asks for context and counts as spend
    # that never happened, sitting next to a duplicate of the payment that did.
    #
    # It is auto-skipped rather than dropped: the row is a true record of an
    # attempt, and skipped transactions are excluded from every total and from
    # classification.
    if monzo_data.data.decline_reason:
        try:
            record_context(con, transaction_id, f"Declined ({monzo_data.data.decline_reason})",
                           skipped=True, source="rule")
            log.info(
                f"Declined transaction auto-skipped: {transaction_id} "
                f"({monzo_data.data.decline_reason}) — no notification sent"
            )
        except Exception as e:
            log.error(f"Failed to auto-skip declined {transaction_id}: {e}", exc_info=True)
        return {"status": "declined"}

    # A provisional hold (see MonzoInner.amount_is_pending) shown to the user as
    # a real charge would be a false alarm -- a UK fuel pre-auth often sits at
    # ~£99/£100 regardless of how much fuel is actually taken. The real, final
    # amount is on its way as a separate transaction with its own id, so this
    # one is auto-skipped the same way a decline is: recorded but never surfaced.
    if monzo_data.data.amount_is_pending:
        try:
            record_context(con, transaction_id, "Pending authorisation hold (amount not final)",
                           skipped=True, source="rule")
            log.info(f"Pending-amount transaction auto-skipped: {transaction_id} — no notification sent")
        except Exception as e:
            log.error(f"Failed to auto-skip pending-amount {transaction_id}: {e}", exc_info=True)
        return {"status": "pending_hold"}

    # Monzo fires several webhooks per payment within half a second, in no set
    # order. Since mid-October 2026 the first has usually been a
    # transaction.updated carrying merchant: null -- and the rules and the card
    # are decided now, from this one, before the others arrive. So a card
    # payment that arrives without a merchant is looked up in Monzo's API,
    # which has it, and the answer kept as an update (the first version stays
    # as it arrived). A transfer has no merchant to find (it has a
    # counterparty), so it isn't looked up. If the API can't be reached, this
    # carries on as it arrived, and the hourly repair tries again
    # (follow_up_tg.run_merchant_repair).
    if not monzo_data.data.merchant and not (monzo_data.data.counterparty or {}).get("name"):
        fetched = await asyncio.to_thread(fetch_transaction, transaction_id)
        if fetched and isinstance(fetched.get("merchant"), dict):
            try:
                full = {"type": monzo_data.type, "data": fetched}
                monzo_data = MonzoOuter.model_validate(full)
                record_update(con, transaction_id, full, "api")
                body = json.dumps(full).encode()
                log.info(f"Merchant filled in from Monzo's API for {transaction_id}: "
                         f"{fetched['merchant'].get('name')!r}")
            except Exception as e:
                log.warning(f"Couldn't use Monzo's API copy of {transaction_id}, keeping the webhook's: {e}")

    # Rules: an auto-context, an auto-skip, a fixed placement, or any of those.
    match = NO_MATCH
    try:
        match = check_rules(monzo_data.data)
    except Exception as e:
        log.error(f"Rule check failed for {transaction_id}, falling back to no match: {e}", exc_info=True)
    if match.matched:
        # A fixed-placement rule with no context of its own still leaves a
        # readable one -- the same "Category - Subcategory" label a quick-tap
        # writes, so the dashboards and exports show something meaningful.
        context = match.context or (f"{match.category} - {match.subcategory}" if match.category else None)
        try:
            record_context(con, transaction_id, context, skipped=match.skip, source="rule",
                           clear_rule_label=False)
            if match.subcategory_id:
                record_label(con, transaction_id, match.parent_id, match.subcategory_id,
                             source="rule", model="rule")
            log.info(
                f"Transaction auto-enriched by rule: {transaction_id} → context='{context}' skip={match.skip}"
                + (f" placement='{match.category} / {match.subcategory}'" if match.category else "")
            )
        except Exception as e:
            log.error(f"Failed to auto-enrich transaction {transaction_id}: {e}", exc_info=True)
        return {"status": "ok"}

    # Ask what it was.
    if bot:
        # Direct debits and bank transfers rarely populate merchant, only
        # counterparty -- without this fallback, quick-tap suggestions for a
        # recurring bill (rent, HMRC, utilities) never surface, and every one
        # of those falls back to the generic top-5 instead of its own history.
        merchant_name = (monzo_data.data.merchant or {}).get('name') or (monzo_data.data.counterparty or {}).get('name')
        try:
            quick_categories = get_quick_categories(merchant_name)
        except Exception as e:
            log.warning(f"Failed to fetch quick categories for {transaction_id}, sending without them: {e}")
            quick_categories = []
        log.info(f"Initial send for {transaction_id}: merchant={merchant_name!r}, quick_categories={quick_categories}")
        # In a thread, because a send that has to be retried waits. Recorded
        # only once it has gone; if it hasn't, the follow-up loop sends it again
        # (follow_up_tg.MISSED_CARD_AFTER).
        try:
            await asyncio.to_thread(bot.send_card, json.loads(body), quick_categories=quick_categories)
            record_request(con, transaction_id)
        except Exception as e:
            log.error(f"Failed to send Telegram notification for {transaction_id}: {e}", exc_info=True)

    return {"status": "ok"}


def _from_owner(update: "TelegramUpdate") -> bool:
    """Whether an update came from the one person this bot works for.
    TELEGRAM_CHAT_ID is your personal chat id, which in a private chat is
    the same number as your user id -- so a button tap is checked by who
    tapped it, and a message by the chat it was sent in."""
    owner = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not owner:
        return False
    if update.callback_query:
        return str(update.callback_query.from_user.id) == owner
    if update.message:
        return str(update.message.chat.id) == owner
    return False


@app.post('/recieve_telegram/')
async def recieve_telegram(request: Request):
    # Telegram sends the secret_token set via setWebhook in this header on
    # every update -- without it, anyone could post a fake button tap here.
    if not _secret_matches(TELEGRAM_WEBHOOK_SECRET, request.headers.get("x-telegram-bot-api-secret-token")):
        log.warning("Rejected a Telegram update with a missing or wrong secret token")
        raise HTTPException(status_code=403, detail="Forbidden")
    body = await request.body()
    try:
        update = TelegramUpdate.model_validate_json(body)
    except Exception as e:
        log.error(f"Telegram parse error: {e}. Body: {body.decode()}")
        return {"ok": True}

    # Anyone can find and message a bot. Only you can act through it -- a
    # quiet 200 for everyone else, so they learn nothing about what it does.
    if not _from_owner(update):
        log.warning("Ignored a Telegram update from someone other than TELEGRAM_CHAT_ID")
        return {"ok": True}

    if update.callback_query:
        cq = update.callback_query
        chat_id = cq.from_user.id
        bot.ack_callback(cq.id)

        if cq.data and cq.data.startswith("enrich:"):
            transaction_id = cq.data.split(":", 1)[1]
            if chat_id in _sessions:
                _sessions[chat_id].get("timeout_task") and _sessions[chat_id]["timeout_task"].cancel()
            task = asyncio.create_task(_enrich_timeout(chat_id, transaction_id))
            _sessions[chat_id] = {"transaction_id": transaction_id, "timeout_task": task}
            label = _txn_short_label(get_con(), transaction_id)
            bot.send_message(chat_id, f"Enriching <b>{html.escape(label)}</b>\n\nWhat was this transaction? Send a one-line description.")

        elif cq.data and cq.data.startswith("skip_confirm:"):
            transaction_id = cq.data.split(":", 1)[1]
            if chat_id in _pending_skips:
                _pending_skips[chat_id].get("timeout_task") and _pending_skips[chat_id]["timeout_task"].cancel()
            message_id = bot.send_skip_confirm(chat_id, transaction_id)
            task = asyncio.create_task(_skip_timeout(chat_id, message_id))
            _pending_skips[chat_id] = {"message_id": message_id, "timeout_task": task}

        elif cq.data and cq.data.startswith("skip_do:"):
            transaction_id = cq.data.split(":", 1)[1]
            if chat_id in _pending_skips:
                _pending_skips[chat_id].get("timeout_task") and _pending_skips[chat_id]["timeout_task"].cancel()
                del _pending_skips[chat_id]
            try:
                record_context(get_con(), transaction_id, "Skipped", skipped=True, source="telegram")
                log.info(f"Transaction skipped via Telegram: {transaction_id}")
            except Exception as e:
                log.error(f"Failed to skip transaction {transaction_id}: {e}", exc_info=True)
                bot.send_message(chat_id, "Something went wrong. Try again.")
                return {"ok": True}
            bot.send_message(chat_id, "⏭ Skipped.")

        elif cq.data and cq.data.startswith("taxprop:"):
            # Only the decision is recorded here. Creating the category and
            # moving the transactions happens on the next pipeline run.
            _, decision, proposal_id = cq.data.split(":", 2)
            try:
                con = get_con()
                row = con.execute(
                    "SELECT proposed_sub, status FROM taxonomy_proposals WHERE id = %s", [int(proposal_id)]
                ).fetchone()
                if not row:
                    bot.send_message(chat_id, "That suggestion has expired.")
                    return {"ok": True}
                proposed_sub, current = row
                if current != "pending":
                    bot.send_message(chat_id, f"Already {current}: <b>{proposed_sub}</b>")
                    return {"ok": True}
                con.execute(
                    "UPDATE taxonomy_proposals SET status = %s, decided_at = %s WHERE id = %s",
                    ["approved" if decision == "approve" else "denied", now(), int(proposal_id)]
                )
            except Exception as e:
                log.error(f"Failed to record taxonomy decision {proposal_id}: {e}", exc_info=True)
                bot.send_message(chat_id, "Something went wrong. Try again.")
                return {"ok": True}
            if decision == "approve":
                bot.send_message(
                    chat_id,
                    f"✅ Approved <b>{proposed_sub}</b>.\n\n"
                    "It'll be created and the listed transactions moved on the next daily run."
                )
            else:
                bot.send_message(
                    chat_id,
                    f"❌ Denied <b>{proposed_sub}</b>. Nothing changed, and it won't be suggested again."
                )
            log.info(f"Taxonomy proposal {proposal_id} ({proposed_sub}) {decision}d via Telegram")

        elif cq.data and cq.data.startswith("catprop:"):
            # catprop:select:<id>:<option_index>, catprop:regenerate:<id>, or
            # catprop:denyall:<id>. Only the decision is recorded here --
            # creating a category or asking Claude for fresh options happens on
            # the next pipeline run.
            parts = cq.data.split(":")
            action = parts[1]
            proposal_id = int(parts[2])
            option_index = int(parts[3]) if action == "select" else None
            try:
                con = get_con()
                row = con.execute(
                    "SELECT options, status FROM category_proposals WHERE id = %s", [proposal_id]
                ).fetchone()
                if not row:
                    bot.send_message(chat_id, "That suggestion has expired.")
                    return {"ok": True}
                options, current = row
                if current != "pending":
                    bot.send_message(chat_id, f"Already {current}.")
                    return {"ok": True}
                if action == "select":
                    con.execute(
                        "UPDATE category_proposals SET status = 'selected', selected_option = %s, decided_at = %s WHERE id = %s",
                        [option_index, now(), proposal_id]
                    )
                elif action == "regenerate":
                    # Recorded as "denied", but regenerate_requested keeps these
                    # options off the permanent denied-name blocklist, since
                    # asking for something different isn't a verdict that these
                    # were wrong -- and it keeps the hold on the transactions
                    # until the fresh proposal takes them over.
                    con.execute(
                        "UPDATE category_proposals SET status = 'denied', regenerate_requested = TRUE, decided_at = %s WHERE id = %s",
                        [now(), proposal_id]
                    )
                else:
                    con.execute(
                        "UPDATE category_proposals SET status = 'denied', decided_at = %s WHERE id = %s",
                        [now(), proposal_id]
                    )
            except Exception as e:
                log.error(f"Failed to record category decision {proposal_id}: {e}", exc_info=True)
                bot.send_message(chat_id, "Something went wrong. Try again.")
                return {"ok": True}
            if action == "select":
                chosen = options[option_index]
                bot.send_message(
                    chat_id,
                    f"✅ Selected <b>{chosen['parent_name']} › {chosen['subcategory_name']}</b>.\n\n"
                    "It'll be created and the waiting transaction(s) classified on the next run."
                )
            elif action == "regenerate":
                bot.send_message(chat_id, "🔄 Got it — you'll get a fresh set of options on the next run.")
            else:
                bot.send_message(
                    chat_id,
                    "❌ None of these. Those transaction(s) will be reconsidered on the next run with "
                    "these ideas ruled out."
                )
            log.info(
                f"Category proposal {proposal_id} {action}"
                + (f" (option {option_index})" if action == "select" else "") + " via Telegram"
            )

        elif cq.data and cq.data.startswith("rulesug:"):
            _, action, sid = cq.data.split(":", 2)
            suggestion_id = int(sid)
            try:
                con = get_con()
                row = con.execute(
                    "SELECT match_field, match_value, auto_context, status, category, subcategory FROM rule_suggestions WHERE id = %s",
                    [suggestion_id]
                ).fetchone()
                if not row:
                    bot.send_message(chat_id, "That suggestion no longer exists.")
                    return {"ok": True}
                match_field, match_value, auto_context, current, category, subcategory_name = row
                if current != "pending":
                    bot.send_message(chat_id, f"Already {current}.")
                    return {"ok": True}
                if action == "approve":
                    # The placement is fixed, not left to the classifier: the
                    # pattern was never once categorized differently, and was
                    # judged single-purpose before being suggested at all.
                    sub_id = subcategory_id(con, category, subcategory_name)
                    if sub_id is None:
                        bot.send_message(
                            chat_id,
                            f"<b>{html.escape(category)} › {html.escape(subcategory_name)}</b> doesn't exist any "
                            "more, so no rule was made. It'll be suggested again if it still fits."
                        )
                        con.execute("DELETE FROM rule_suggestions WHERE id = %s", [suggestion_id])
                        return {"ok": True}
                    next_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM rules").fetchone()[0]
                    con.execute(
                        """INSERT INTO rules (id, name, match_field, match_type, match_value, auto_context, subcategory_id)
                           VALUES (%s, %s, %s, 'exact', %s, %s, %s)""",
                        [next_id, f"Suggested: {match_value}", match_field, match_value, auto_context, sub_id]
                    )
                    con.execute(
                        "UPDATE rule_suggestions SET status = 'approved', decided_at = %s WHERE id = %s",
                        [now(), suggestion_id]
                    )
                else:
                    con.execute(
                        "UPDATE rule_suggestions SET status = 'denied', decided_at = %s WHERE id = %s",
                        [now(), suggestion_id]
                    )
            except Exception as e:
                log.error(f"Failed to record rule suggestion decision {suggestion_id}: {e}", exc_info=True)
                bot.send_message(chat_id, "Something went wrong. Try again.")
                return {"ok": True}
            if action == "approve":
                bot.send_message(
                    chat_id,
                    f"✅ Rule created — <b>{html.escape(match_value)}</b> will go straight to "
                    f"<b>{html.escape(category)} › {html.escape(subcategory_name)}</b> from now on."
                )
            else:
                bot.send_message(chat_id, "👍 Won't suggest that one again.")
            log.info(f"Rule suggestion {suggestion_id} {action}d via Telegram")

        elif cq.data and cq.data.startswith("qcat:"):
            # qcat:<transaction>:<subcategory id>. The id rather than a position
            # in a list, so a tap files exactly what the button said even if the
            # quick categories have changed since the card was sent.
            _, transaction_id, sub = cq.data.split(":", 2)
            if chat_id in _sessions:
                _sessions[chat_id].get("timeout_task") and _sessions[chat_id]["timeout_task"].cancel()
                del _sessions[chat_id]
            try:
                con = get_con()
                found = subcategory(con, int(sub))
                if not found:
                    bot.send_message(chat_id, "That category doesn't exist any more. Tap ✏️ Enrich instead.")
                    return {"ok": True}
                _, category, subcategory_name = found
                # The same "Category - Subcategory" a quick-tap has always
                # written; the pipeline files it from that, after the placement
                # judge has had its look.
                context = f"{category} - {subcategory_name}"
                record_context(con, transaction_id, context, source="telegram")
                log.info(f"Transaction quick-categorized via Telegram: {transaction_id} -> {context}")
            except Exception as e:
                log.error(f"Failed to quick-categorize transaction {transaction_id}: {e}", exc_info=True)
                bot.send_message(chat_id, "Something went wrong. Try again.")
                return {"ok": True}
            bot.send_message(chat_id, f"✅ Saved as {category} → {subcategory_name}")

        elif cq.data and cq.data.startswith("quickcat:"):
            # A button from a card sent before the move. Its number pointed at a
            # row of a table that no longer exists, so it can't be trusted to
            # mean the category it showed.
            bot.send_message(chat_id, "That button is from before an update. Tap ✏️ Enrich instead.")

        elif cq.data == "skip_cancel":
            if chat_id in _pending_skips:
                _pending_skips[chat_id].get("timeout_task") and _pending_skips[chat_id]["timeout_task"].cancel()
                del _pending_skips[chat_id]
            bot.send_message(chat_id, "Cancelled.")

    elif update.message and update.message.text:
        msg = update.message
        chat_id = msg.chat.id
        session = _sessions.get(chat_id)

        if not session:
            return {"ok": True}

        session.get("timeout_task") and session["timeout_task"].cancel()
        context = msg.text.strip()
        transaction_id = session["transaction_id"]

        try:
            record_context(get_con(), transaction_id, context, source="telegram")
            log.info(f"Transaction enriched: {transaction_id}")
        except Exception as e:
            log.error(f"Failed to enrich transaction {transaction_id}: {e}", exc_info=True)
            bot.send_message(chat_id, "Something went wrong saving that. Try again.")
            return {"ok": True}

        del _sessions[chat_id]
        bot.send_message(
            chat_id,
            f"✅ <code>{transaction_id}</code> saved.\n\n📝 {context}",
            reply_markup={"inline_keyboard": [[{"text": "✏️ Edit", "callback_data": f"enrich:{transaction_id}"}]]}
        )

    return {"ok": True}


# Where a transaction is up to, worked out from its events: no answer yet,
# answered but not yet classified, classified, or skipped.
STATUS_SQL = """CASE WHEN t.skipped THEN 'skipped'
                     WHEN t.enriched_at IS NULL THEN 'pending'
                     WHEN t.llm_subcategory IS NULL THEN 'enriched'
                     ELSE 'classified' END"""


def _amount_str(amount_pence: int) -> str:
    return f"-£{abs(amount_pence) / 100:.2f}" if amount_pence < 0 else f"+£{amount_pence / 100:.2f}"


@app.get("/dashboard/transaction/{transaction_id}", response_class=HTMLResponse)
async def transaction_detail(transaction_id: str, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    row = get_con().execute(
        f"""SELECT t.id, t.raw_payload, t.received_at, {STATUS_SQL}, t.user_context, t.enriched_at, t.skipped
            FROM transactions t WHERE t.id = %s""",
        [transaction_id]
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Transaction not found")

    payload = row[1]
    data = payload.get("data", {})
    amount_pence = data.get("amount", 0)
    return templates.TemplateResponse(
        request=request,
        name="transaction.html",
        context={
            "transaction_id": row[0],
            "received_at": row[2],
            "status": row[3],
            "user_context": row[4],
            "enriched_at": row[5],
            "skipped": row[6],
            "amount": _amount_str(amount_pence),
            "is_debit": amount_pence < 0,
            "description": data.get("description", ""),
            "category": data.get("category", ""),
            "currency": data.get("currency", ""),
            "created": data.get("created", ""),
            "settled": data.get("settled") or None,
            "is_load": data.get("is_load"),
            "merchant": data.get("merchant"),
            "counterparty": data.get("counterparty"),
            "raw": json.dumps(payload, indent=2),
        }
    )


def _exists(con, transaction_id: str) -> bool:
    return con.execute("SELECT 1 FROM transactions_raw WHERE id = %s", [transaction_id]).fetchone() is not None


@app.post("/dashboard/transaction/{transaction_id}/skip", response_class=HTMLResponse)
async def skip_transaction(transaction_id: str, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    con = get_con()
    if not _exists(con, transaction_id):
        raise HTTPException(status_code=404, detail="Transaction not found")
    record_context(con, transaction_id, "Skipped", skipped=True, source="dashboard")
    return RedirectResponse(url=f"/dashboard/transaction/{transaction_id}", status_code=303)


@app.post("/dashboard/transaction/{transaction_id}/unskip", response_class=HTMLResponse)
async def unskip_transaction(transaction_id: str, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    """Count it again, with no context: the classifier picks it up on the next
    run. Add context first (or after) if it needs explaining."""
    con = get_con()
    if not _exists(con, transaction_id):
        raise HTTPException(status_code=404, detail="Transaction not found")
    record_context(con, transaction_id, None, skipped=False, source="dashboard")
    return RedirectResponse(url=f"/dashboard/transaction/{transaction_id}", status_code=303)


@app.post("/dashboard/transaction/{transaction_id}/enrich-dashboard", response_class=HTMLResponse)
async def enrich_transaction_dashboard(transaction_id: str, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    form = await request.form()
    context = (form.get("context") or "").strip()
    if not context:
        return RedirectResponse(url="/dashboard", status_code=303)
    con = get_con()
    if not _exists(con, transaction_id):
        raise HTTPException(status_code=404, detail="Transaction not found")
    record_context(con, transaction_id, context, source="dashboard")
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/dashboard/transaction/{transaction_id}/edit-context", response_class=HTMLResponse)
async def edit_transaction_context(transaction_id: str, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    form = await request.form()
    context = (form.get("context") or "").strip()
    con = get_con()
    if context and _exists(con, transaction_id):
        record_context(con, transaction_id, context, source="dashboard")
    return RedirectResponse(url=f"/dashboard/transaction/{transaction_id}", status_code=303)


@app.post("/dashboard/transaction/{transaction_id}/delete", response_class=HTMLResponse)
async def delete_transaction(transaction_id: str, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    """Gone for good, with its history -- for a test payment or a mistake,
    not for something you'd rather not see (skip that instead)."""
    con = get_con()
    with con.transaction():
        con.execute("DELETE FROM label_events WHERE txn_id = %s", [transaction_id])
        con.execute("DELETE FROM context_events WHERE txn_id = %s", [transaction_id])
        con.execute("DELETE FROM judge_reviews WHERE txn_id = %s", [transaction_id])
        con.execute("DELETE FROM transaction_updates WHERE txn_id = %s", [transaction_id])
        con.execute("DELETE FROM transactions_raw WHERE id = %s", [transaction_id])
    log.info(f"Transaction deleted from the dashboard: {transaction_id}")
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/dashboard/rules/test", response_class=HTMLResponse)
async def test_rules(request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    form = await request.form()
    con = get_con()
    context = {"rules": _get_rules(con), "transactions": _get_test_transactions(con),
               "taxonomy": _get_taxonomy(con), "active_nav": "rules"}

    # "Test this rule" on the Add Rule form: run the not-yet-saved rule against
    # recent transactions, so you see which real ones it would catch -- not
    # whether it matches a single transaction you already picked it from.
    candidate_field = (form.get("candidate_match_field") or "").strip()
    if candidate_field:
        candidate = {
            "match_field": candidate_field,
            "match_type": (form.get("candidate_match_type") or "").strip(),
            "match_value": (form.get("candidate_match_value") or "").strip(),
            "match_field_2": (form.get("candidate_match_field_2") or "").strip() or None,
            "match_type_2": (form.get("candidate_match_type_2") or "").strip() or None,
            "match_value_2": (form.get("candidate_match_value_2") or "").strip() or None,
        }
        recent = _get_test_transactions(con, limit=None)
        matches = [t for t in recent if _evaluate_rule(t, **candidate)]
        context["candidate_results"] = {
            "rule": {**candidate,
                     "name": (form.get("candidate_name") or "").strip(),
                     "auto_context": (form.get("candidate_auto_context") or "").strip(),
                     "auto_skip": (form.get("candidate_auto_skip") or "") == "on",
                     "category": (form.get("candidate_category") or "").strip(),
                     "subcategory": (form.get("candidate_subcategory") or "").strip()},
            "queue_size": len(recent),
            "matches": matches,
        }
        return templates.TemplateResponse(request=request, name="rules.html", context=context)

    # "▶ Test": which saved rules match one transaction's fields.
    test_data = {
        "merchant_name": (form.get("merchant_name") or "").strip(),
        "description": (form.get("description") or "").strip(),
        "counterparty_name": (form.get("counterparty_name") or "").strip(),
        "category": (form.get("category") or "").strip(),
        "scheme": (form.get("scheme") or "").strip(),
        "amount": (form.get("amount") or "0").strip(),
    }
    try:
        amount_pence = int(round(float(test_data["amount"]) * 100))
    except ValueError:
        amount_pence = 0
    fields = {**test_data, "amount_pence": amount_pence}

    results = []
    for rule in _get_rules(con):
        matched = bool(rule["enabled"]) and _evaluate_rule(
            fields, rule["match_field"], rule["match_type"], rule["match_value"],
            rule["match_field_2"], rule["match_type_2"], rule["match_value_2"],
        )
        results.append({
            "id": rule["id"], "name": rule["name"], "enabled": rule["enabled"],
            "matched": matched, "auto_context": rule["auto_context"], "auto_skip": bool(rule["auto_skip"]),
            "category": rule["category"], "subcategory": rule["subcategory"],
        })

    context.update({"test_results": results, "test_data": test_data})
    return templates.TemplateResponse(request=request, name="rules.html", context=context)


def _rule_form(con, form) -> dict | None:
    """The rule a form describes, or None if it isn't a usable one: it needs a
    name and a first condition, and has to do something -- give context, skip,
    or file into a category."""
    rule = {
        "name": (form.get("name") or "").strip(),
        "match_field": (form.get("match_field") or "").strip(),
        "match_type": (form.get("match_type") or "").strip(),
        "match_value": (form.get("match_value") or "").strip(),
        "auto_context": (form.get("auto_context") or "").strip(),
        "match_field_2": (form.get("match_field_2") or "").strip() or None,
        "match_type_2": (form.get("match_type_2") or "").strip() or None,
        "match_value_2": (form.get("match_value_2") or "").strip() or None,
        "auto_skip": form.get("auto_skip") == "on",
        "subcategory_id": _rule_placement(con, form),
    }
    if not (rule["name"] and rule["match_field"] and rule["match_type"] and rule["match_value"]):
        return None
    if not (rule["auto_context"] or rule["auto_skip"] or rule["subcategory_id"]):
        return None
    return rule


@app.post("/dashboard/rules/{rule_id}/edit", response_class=HTMLResponse)
async def edit_rule(rule_id: int, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    con = get_con()
    rule = _rule_form(con, await request.form())
    if rule:
        con.execute(
            """UPDATE rules SET name = %(name)s, match_field = %(match_field)s, match_type = %(match_type)s,
                   match_value = %(match_value)s, auto_context = %(auto_context)s,
                   match_field_2 = %(match_field_2)s, match_type_2 = %(match_type_2)s,
                   match_value_2 = %(match_value_2)s, auto_skip = %(auto_skip)s,
                   subcategory_id = %(subcategory_id)s
               WHERE id = %(id)s""",
            {**rule, "id": rule_id}
        )
    return RedirectResponse(url="/dashboard/rules", status_code=303)


@app.post("/dashboard/rules/{rule_id}/delete", response_class=HTMLResponse)
async def delete_rule(rule_id: int, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    get_con().execute("DELETE FROM rules WHERE id = %s", [rule_id])
    return RedirectResponse(url="/dashboard/rules", status_code=303)


@app.post("/dashboard/rules/add", response_class=HTMLResponse)
async def add_rule(request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    con = get_con()
    rule = _rule_form(con, await request.form())
    if rule:
        next_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM rules").fetchone()[0]
        con.execute(
            """INSERT INTO rules (id, name, match_field, match_type, match_value, auto_context,
                                  match_field_2, match_type_2, match_value_2, auto_skip, subcategory_id)
               VALUES (%(id)s, %(name)s, %(match_field)s, %(match_type)s, %(match_value)s, %(auto_context)s,
                       %(match_field_2)s, %(match_type_2)s, %(match_value_2)s, %(auto_skip)s, %(subcategory_id)s)""",
            {**rule, "id": next_id}
        )
    return RedirectResponse(url="/dashboard/rules", status_code=303)


def _rule_placement(con, form) -> int | None:
    """The fixed placement a rule form picked, as a subcategory id, or None.
    Only a complete pair of a live category counts -- a category picked
    without a subcategory would file transactions under a parent with nothing
    beneath it, so a half-filled pair is treated as no placement at all."""
    category = (form.get("category") or "").strip()
    subcategory_name = (form.get("subcategory") or "").strip()
    if category and subcategory_name:
        return subcategory_id(con, category, subcategory_name)
    return None


def _get_taxonomy(con) -> dict[str, list[str]]:
    """{category: [subcategory, ...]} of live categories, for the Rules page's
    placement dropdowns."""
    taxonomy: dict[str, list[str]] = {}
    for category, subcategory_name in con.execute(
        """SELECT p.name, s.name FROM subcategories s JOIN parent_categories p ON p.id = s.parent_id
           WHERE s.archived_at IS NULL AND p.archived_at IS NULL ORDER BY p.name, s.name"""
    ).fetchall():
        taxonomy.setdefault(category, []).append(subcategory_name)
    return taxonomy


def _get_rules(con):
    rows = con.execute(
        """SELECT r.id, r.name, r.match_field, r.match_type, r.match_value, r.auto_context, r.enabled,
                  r.match_field_2, r.match_type_2, r.match_value_2, r.auto_skip, p.name, s.name
           FROM rules r
           LEFT JOIN subcategories s ON s.id = r.subcategory_id
           LEFT JOIN parent_categories p ON p.id = s.parent_id
           ORDER BY r.id"""
    ).fetchall()
    return [
        {"id": r[0], "name": r[1], "match_field": r[2], "match_type": r[3],
         "match_value": r[4], "auto_context": r[5], "enabled": r[6],
         "match_field_2": r[7], "match_type_2": r[8], "match_value_2": r[9], "auto_skip": r[10],
         "category": r[11], "subcategory": r[12]}
        for r in rows
    ]


def _display(data: dict, fallback: str) -> str:
    merchant = (data.get("merchant") or {}).get("name") or ""
    counterparty = (data.get("counterparty") or {}).get("name") or ""
    return merchant or counterparty or data.get("description") or fallback


def _txn_short_label(con, transaction_id: str) -> str:
    """One line identifying a transaction in a chat message -- merchant (or
    counterparty, or description, whichever is actually populated) plus the
    signed amount, so "Enriching tx_0000..." doesn't leave the user guessing
    which of several pending transactions they just tapped."""
    stored = payload(con, transaction_id)
    if not stored:
        return transaction_id
    data = stored.get("data", {})
    amount_pence = data.get("amount", 0)
    sign = "-" if amount_pence < 0 else "+"
    return f"{_display(data, transaction_id)} · {sign}£{abs(amount_pence) / 100:.2f}"


def _get_test_transactions(con, limit: int | None = 100):
    """Recent transactions as the rule tester sees them: the newest `limit`, or
    the last 30 days for testing a rule that hasn't been saved yet."""
    sql = f"""SELECT t.id, t.raw_payload, t.received_at, t.user_context, {STATUS_SQL}
              FROM transactions t"""
    if limit:
        rows = con.execute(sql + " ORDER BY t.received_at DESC LIMIT %s", [int(limit)]).fetchall()
    else:
        rows = con.execute(
            sql + " WHERE t.received_at >= LOCALTIMESTAMP - INTERVAL '30 days' ORDER BY t.received_at DESC"
        ).fetchall()
    txns = []
    for row in rows:
        data = row[1].get("data", {})
        amount_pence = data.get("amount", 0)
        sign = "-" if amount_pence < 0 else "+"
        txns.append({
            "id": row[0],
            "label": f"{_display(data, row[0])} · {sign}£{abs(amount_pence) / 100:.2f} · {str(row[2])[:10]}",
            "merchant_name": (data.get("merchant") or {}).get("name") or "",
            "description": data.get("description") or "",
            "counterparty_name": (data.get("counterparty") or {}).get("name") or "",
            "category": data.get("category") or "",
            "scheme": data.get("scheme") or "",
            # Signed, not abs() -- a candidate amount_negative/amount_positive
            # rule can only be tested meaningfully against the real direction
            # the money moved.
            "amount": round(amount_pence / 100, 2),
            "amount_pence": amount_pence,
            "user_context": row[3] or "",
            "status": row[4],
        })
    return txns


def _evaluate_rule(fields: dict, match_field, match_type, match_value,
                   match_field_2=None, match_type_2=None, match_value_2=None) -> bool:
    """Whether a rule's condition(s) match one transaction's fields -- the
    same keys _get_test_transactions produces, with amount in signed pence.
    Shared by the saved-rule tester and the preview for a new rule, so both
    judge a match exactly the same way."""
    from check_rules import _matches

    def get_field(field):
        if field == "amount":
            return fields.get("amount_pence")
        return fields.get(field) or None

    try:
        if not (match_field and match_type and match_value):
            return False
        v1 = get_field(match_field)
        if v1 is None or not _matches(v1, match_type, match_value):
            return False
        if match_field_2 and match_type_2 and match_value_2:
            v2 = get_field(match_field_2)
            if v2 is None or not _matches(v2, match_type_2, match_value_2):
                return False
        return True
    except Exception:
        return False


@app.get("/dashboard/rules", response_class=HTMLResponse)
async def rules_view(request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    con = get_con()
    return templates.TemplateResponse(
        request=request,
        name="rules.html",
        context={"rules": _get_rules(con), "transactions": _get_test_transactions(con),
                 "taxonomy": _get_taxonomy(con), "active_nav": "rules"}
    )


@app.post("/dashboard/rules/{rule_id}/toggle", response_class=HTMLResponse)
async def toggle_rule(rule_id: int, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    get_con().execute("UPDATE rules SET enabled = NOT enabled WHERE id = %s", [rule_id])
    return RedirectResponse(url="/dashboard/rules", status_code=303)


DB_PAGE_SIZE = 50

@app.get("/dashboard/db", response_class=HTMLResponse)
async def db_view(request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials),
                  raw_page: int = Query(1, ge=1)):
    con = get_con()

    def table(name, sql, columns, params=(), total=None, page=1, page_param=None):
        rows = con.execute(sql, params).fetchall()
        total = len(rows) if total is None else total
        return {"name": name, "columns": columns, "rows": rows, "total": total, "page": page,
                "total_pages": max(1, -(-total // DB_PAGE_SIZE)) if page_param else 1, "page_param": page_param}

    raw_total = con.execute("SELECT COUNT(*) FROM transactions_raw").fetchone()[0]
    tables = [
        table("transactions_raw", "SELECT id, received_at, payload FROM transactions_raw "
              "ORDER BY received_at DESC LIMIT %s OFFSET %s", ["id", "received_at", "payload"],
              [DB_PAGE_SIZE, (raw_page - 1) * DB_PAGE_SIZE], total=raw_total, page=raw_page, page_param="raw_page"),
        table("context_events (latest 50)",
              "SELECT event_id, txn_id, user_context, skipped, created_at_override, source, set_at "
              "FROM context_events ORDER BY event_id DESC LIMIT 50",
              ["event_id", "txn_id", "user_context", "skipped", "created_at_override", "source", "set_at"]),
        table("label_events (latest 50)",
              "SELECT e.event_id, e.txn_id, p.name, s.name, e.source, e.model, e.confidence, e.set_at "
              "FROM label_events e LEFT JOIN parent_categories p ON p.id = e.parent_id "
              "LEFT JOIN subcategories s ON s.id = e.subcategory_id ORDER BY e.event_id DESC LIMIT 50",
              ["event_id", "txn_id", "category", "subcategory", "source", "model", "confidence", "set_at"]),
        table("rules",
              "SELECT r.id, r.name, r.match_field, r.match_type, r.match_value, r.auto_context, r.enabled, "
              "r.match_field_2, r.match_type_2, r.match_value_2, r.auto_skip, p.name, s.name FROM rules r "
              "LEFT JOIN subcategories s ON s.id = r.subcategory_id "
              "LEFT JOIN parent_categories p ON p.id = s.parent_id ORDER BY r.id",
              ["id", "name", "match_field", "match_type", "match_value", "auto_context", "enabled",
               "match_field_2", "match_type_2", "match_value_2", "auto_skip", "category", "subcategory"]),
        table("quick_categories",
              "SELECT merchant_name, subcategory_id, category, subcategory, rank FROM quick_categories "
              "ORDER BY merchant_name NULLS FIRST, rank",
              ["merchant_name", "subcategory_id", "category", "subcategory", "rank"]),
    ]
    return templates.TemplateResponse(request=request, name="db.html",
                                      context={"tables": tables, "active_nav": "db"})


PAGE_SIZE = 20

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(verify_credentials),
    page: int = Query(1, ge=1),
    status_filter: str = Query("", alias="status"),
    search: str = Query(""),
):
    con = get_con()
    totals = con.execute(
        """SELECT (SELECT COUNT(*) FROM transactions_raw),
                  (SELECT COALESCE(SUM(ABS((payload->'data'->>'amount')::BIGINT)), 0) FROM transactions_raw),
                  (SELECT COUNT(*) FROM context_events WHERE source = 'request'),
                  (SELECT COUNT(*) FROM transactions WHERE enriched_at IS NOT NULL),
                  (SELECT COUNT(*) FROM transactions WHERE llm_subcategory IS NOT NULL)"""
    ).fetchone()
    # "The queue" is what still needs something: an answer, or a category.
    queue_stats = con.execute(
        f"""SELECT status, COUNT(*) FROM (SELECT {STATUS_SQL} AS status FROM transactions t) s
            WHERE status IN ('pending', 'enriched') GROUP BY status ORDER BY status"""
    ).fetchall()

    if status_filter in ("pending", "enriched", "skipped", "classified"):
        where_clauses, params = ["status = %s"], [status_filter]
    else:
        where_clauses, params = ["status IN ('pending', 'enriched')"], []
    if search:
        where_clauses.append("(id ILIKE %s OR raw_payload::TEXT ILIKE %s)")
        params.extend([f"%{search}%", f"%{search}%"])
    listing = f"""SELECT * FROM (
                      SELECT t.id, t.received_at, {STATUS_SQL} AS status, t.raw_payload, t.skipped,
                             (SELECT COUNT(*) FROM context_events e
                              WHERE e.txn_id = t.id AND e.source = 'request') AS request_count
                      FROM transactions t) s
                  WHERE {" AND ".join(where_clauses)}"""
    total_queue = con.execute(f"SELECT COUNT(*) FROM ({listing}) q", params).fetchone()[0]
    rows = con.execute(
        listing + " ORDER BY received_at DESC LIMIT %s OFFSET %s", params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]
    ).fetchall()

    queue = []
    for txn_id, received_at, status_value, raw, skipped, request_count in rows:
        data = raw.get("data", {})
        amount_pence = data.get("amount", 0)
        queue.append({
            "id": txn_id,
            "received_at": received_at,
            "status": status_value,
            "request_count": request_count or 0,
            "amount": _amount_str(amount_pence),
            "is_debit": amount_pence < 0,
            "skipped": bool(skipped),
            "description": data.get("description", ""),
            "merchant": (data.get("merchant") or {}).get("name", "") or (data.get("counterparty") or {}).get("name", ""),
        })

    total_received, total_amount_pence, requests_sent, total_enriched, total_classified = totals
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "total_received": total_received,
            "total_amount": f"£{abs(total_amount_pence) / 100:,.2f}",
            "requests_sent": requests_sent,
            "total_enriched": total_enriched,
            "total_processed": total_classified,
            "queue_stats": queue_stats,
            "queue": queue,
            "page": page,
            "total_pages": max(1, -(-total_queue // PAGE_SIZE)),
            "total_queue": total_queue,
            "status_filter": status_filter,
            "search": search,
            "active_nav": "dashboard",
        }
    )


@app.get("/dashboard/logs", response_class=HTMLResponse)
async def logs_view(request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials), lines: int = Query(200, ge=10, le=2000)):
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        log_lines = all_lines[-lines:]
    except FileNotFoundError:
        log_lines = ["Log file not found."]
    return templates.TemplateResponse(
        request=request,
        name="logs.html",
        context={"log_lines": log_lines, "lines": lines, "active_nav": "logs"}
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
