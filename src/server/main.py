from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, HTTPException, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional
import asyncio
import uvicorn
import html
import logging
import re
import secrets
import json
import os
import sys

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from server_db import (
    init_db, get_con, get_quick_categories, insert_with_next_id, now, payload, record_context, record_label,
    record_request, record_update, subcategory, subcategory_id,
)
from telegram import TelegramBot
from check_rules import check_rules, NO_MATCH
from follow_up_tg import requester_loop
from monzo_oauth import (
    begin_authorization, complete_authorization, fetch_transaction, monzo_refresh_loop, MONZO_CLIENT_ID,
)

BASE_DIR = os.path.dirname(__file__)

# The pipeline's activity log (warnings and errors into the database, for the
# dashboard's Activity page). Last on the path, so nothing there shadows ours.
sys.path.append(os.path.join(BASE_DIR, "..", "pipeline"))
import activity  # noqa: E402

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


# The login in front of /oauth/start, so only you can link a Monzo account.
# Named after the web dashboard it used to guard too (gone: the Streamlit
# dashboard does all of that now); kept so existing .env files still work.
DASHBOARD_USER = os.getenv("DASHBOARD_USER")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
_sessions: dict = {}
_pending_skips: dict = {}

security = HTTPBasic()


def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    if not DASHBOARD_USER or not DASHBOARD_PASSWORD:
        raise HTTPException(status_code=503, detail="Login not configured (DASHBOARD_USER/DASHBOARD_PASSWORD)")
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
    activity.attach("server", get_con)
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


def _public_base_url(request: Request) -> str:
    """https://your-domain, as the outside world sees it. Behind Caddy the app
    itself only ever sees plain http, so the scheme Caddy forwarded wins."""
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{scheme}://{request.headers.get('host', request.url.netloc)}"


@app.get("/oauth/start")
async def oauth_start(request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    """Begin (or redo) Monzo authorization: sends you to Monzo's login with a
    fresh single-use state. Behind the login (DASHBOARD_USER), so only you can
    start a flow whose result gets stored."""
    if not MONZO_CLIENT_ID:
        return HTMLResponse("<p>MONZO_CLIENT_ID isn't set on the server -- see SETUP.md.</p>", status_code=503)
    return RedirectResponse(begin_authorization(f"{_public_base_url(request)}/oauth/callback"), status_code=303)


@app.get("/oauth/callback", response_class=HTMLResponse)
async def oauth_callback(code: str = "", state: str = "", error: str = ""):
    """Monzo's OAuth redirect lands here, so it can't sit behind the
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
                           clear_rule_label=False, rule_id=match.rule_id)
            if match.subcategory_id:
                record_label(con, transaction_id, match.parent_id, match.subcategory_id,
                             source="rule", model="rule", rule_id=match.rule_id)
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
                    "It'll be created and the listed transactions moved on the next run, within the hour."
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
                    insert_with_next_id(
                        con,
                        """INSERT INTO rules (id, name, match_field, match_type, match_value, auto_context, subcategory_id)
                           VALUES ((SELECT COALESCE(MAX(id), 0) + 1 FROM rules), %s, %s, 'exact', %s, %s, %s) RETURNING id""",
                        [f"Suggested: {match_value}", match_field, match_value, auto_context, sub_id], "rules",
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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
