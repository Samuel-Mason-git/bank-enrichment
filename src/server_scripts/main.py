from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, HTTPException, Depends, Request, status, Query, Security
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials, APIKeyHeader
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

from server_db import init_db, get_con, get_quick_categories
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

LOCAL_API_KEY = os.environ.get("LOCAL_API_KEY")
API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)
async def verify_api_key(api_key: str = Security(API_KEY_HEADER)):
    if not LOCAL_API_KEY or api_key != LOCAL_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing API key"
        )
    return api_key

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

class MarkProcessedRequest(BaseModel):
    ids: list[str]

class MonthlyReportRequest(BaseModel):
    message: str

class QuickCategoryEntry(BaseModel):
    category: str
    subcategory: str
    merchant_name: Optional[str] = None
    rank: int = 0

class SyncQuickCategoriesRequest(BaseModel):
    entries: list[QuickCategoryEntry]

class TaxonomyEntry(BaseModel):
    category: str
    subcategory: str

class SyncTaxonomyRequest(BaseModel):
    entries: list[TaxonomyEntry]

class TaxonomyProposalEntry(BaseModel):
    id: int                      # the id in the LOCAL database
    parent_name: str
    source_sub: str
    action: str = "create"       # 'create' a new subcategory, or 'move' into one
    target_parent: str = ""
    proposed_sub: str
    rationale: str
    evidence_count: int = 0
    examples: list[str] = []

class SyncTaxonomyProposalsRequest(BaseModel):
    proposals: list[TaxonomyProposalEntry]

class CategoryOption(BaseModel):
    parent_name: str
    subcategory_name: str
    parent_is_new: bool
    rationale: str
    # Set by the local placement judge. 'judge' marks a second-look card (a
    # placement being questioned, not a new category being asked for) and
    # 'is_original' the option that means "leave it where it was". Both default
    # off so an older local side, which sends neither, is unaffected.
    judge: bool = False
    is_original: bool = False

class AlertRequest(BaseModel):
    title: str = Field(max_length=120)
    message: str = Field(max_length=1000)

class CategoryProposalEntry(BaseModel):
    id: int                      # the id in the LOCAL database
    options: list[CategoryOption]
    txn_count: int = 0
    examples: list[str] = []

class SyncCategoryProposalsRequest(BaseModel):
    proposals: list[CategoryProposalEntry]


class RuleSuggestionCandidate(BaseModel):
    match_field: str
    match_value: str
    category: str
    subcategory: str
    txn_count: int = 0
    auto_context: str = ""
    # Not persisted -- only ever shown on the initial card, not needed again
    # once a decision is made.
    llm_reasoning: str = ""

class SyncRuleSuggestionsRequest(BaseModel):
    candidates: list[RuleSuggestionCandidate]

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

    received_at = time.strftime("%Y-%m-%d %H:%M:%S")
    transaction_id = monzo_data.data.id

    # Upload to Queue
    try:
        con = get_con()
        result = con.execute(
            "INSERT INTO webhook_queue (id, payload, received_at) VALUES (?, ?, ?) ON CONFLICT DO NOTHING RETURNING id",
            [transaction_id, body.decode(), received_at]
        ).fetchone()
        is_new = result is not None

        if is_new:
            con.execute(
                "UPDATE stats SET total_received = total_received + 1, total_amount_pence = total_amount_pence + ?, requests_sent = requests_sent + 1 WHERE id = 1",
                [abs(monzo_data.data.amount)]
            )
            log.info(f"Transaction stored: {transaction_id}")
        else:
            log.info(f"Duplicate webhook ignored: {transaction_id}")
    except Exception as e:
        log.error(f"Failed to store transaction {transaction_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to store transaction")

    # A declined payment moved no money, but Monzo fires a webhook for it just
    # like a real one -- and the retry that succeeds arrives seconds later as a
    # separate transaction. Left alone it asks for context, gets pulled into the
    # local database, and counts as spend that never happened, sitting next to a
    # duplicate of the payment that did.
    #
    # It is auto-skipped rather than dropped: the row is a true record of an
    # attempt, it keeps the id in the dedup buffer, and skipped transactions are
    # already excluded from every total and from classification.
    if is_new and monzo_data.data.decline_reason:
        try:
            con.execute(
                """UPDATE webhook_queue
                   SET status = 'enriched', skipped = TRUE, user_context = ?, enriched_at = ?
                   WHERE id = ?""",
                [f"Declined ({monzo_data.data.decline_reason})",
                 time.strftime("%Y-%m-%d %H:%M:%S"), transaction_id]
            )
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
    # one is auto-skipped the same way a decline is: recorded (keeps its id in
    # the dedup buffer) but never surfaced.
    if is_new and monzo_data.data.amount_is_pending:
        try:
            con.execute(
                """UPDATE webhook_queue
                   SET status = 'enriched', skipped = TRUE, user_context = ?, enriched_at = ?
                   WHERE id = ?""",
                ["Pending authorisation hold (amount not final)",
                 time.strftime("%Y-%m-%d %H:%M:%S"), transaction_id]
            )
            log.info(f"Pending-amount transaction auto-skipped: {transaction_id} — no notification sent")
        except Exception as e:
            log.error(f"Failed to auto-skip pending-amount {transaction_id}: {e}", exc_info=True)
        return {"status": "pending_hold"}

    # Monzo fires several webhooks per payment within half a second, in no set
    # order, and only the first is kept. Since mid-October 2026 the first has
    # usually been a transaction.updated carrying merchant: null, so the
    # merchant was lost -- and with it merchant rules, the merchant's own
    # quick-tap buttons, and what the classifier knows. So a card payment that
    # arrives without one is looked up in Monzo's API, which has it, and stored
    # in full. A transfer has no merchant to find (it has a counterparty), so
    # it isn't looked up. If the API can't be reached, this one carries on as
    # it arrived.
    if is_new and not monzo_data.data.merchant and not (monzo_data.data.counterparty or {}).get("name"):
        fetched = await asyncio.to_thread(fetch_transaction, transaction_id)
        if fetched and isinstance(fetched.get("merchant"), dict):
            try:
                full = {"type": monzo_data.type, "data": fetched}
                monzo_data = MonzoOuter.model_validate(full)
                con.execute(
                    "UPDATE webhook_queue SET payload = ? WHERE id = ?", [json.dumps(full), transaction_id]
                )
                log.info(f"Merchant filled in from Monzo's API for {transaction_id}: "
                         f"{fetched['merchant'].get('name')!r}")
            except Exception as e:
                log.warning(f"Couldn't use Monzo's API copy of {transaction_id}, keeping the webhook's: {e}")

    # Check rules for auto-enrichment / auto-skip / a fixed placement
    match = NO_MATCH
    if is_new:
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
            con.execute(
                """UPDATE webhook_queue
                   SET user_context = ?, status = 'enriched', enriched_at = ?, skipped = ?,
                       rule_category = ?, rule_subcategory = ?
                   WHERE id = ?""",
                [context, time.strftime("%Y-%m-%d %H:%M:%S"), match.skip,
                 match.category, match.subcategory, transaction_id]
            )
            con.execute("UPDATE stats SET total_enriched = total_enriched + 1 WHERE id = 1")
            log.info(
                f"Transaction auto-enriched by rule: {transaction_id} → context='{context}' skip={match.skip}"
                + (f" placement='{match.category} / {match.subcategory}'" if match.category else "")
            )
        except Exception as e:
            log.error(f"Failed to auto-enrich transaction {transaction_id}: {e}", exc_info=True)

    # Send Telegram Message
    if is_new and not match.matched and bot:
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
        try:
            bot.send_card(json.loads(body), quick_categories=quick_categories)
            con.execute(
                "UPDATE webhook_queue SET request_count = request_count + 1, last_requested_at = ? WHERE id = ?",
                [time.strftime("%Y-%m-%d %H:%M:%S"), transaction_id]
            )
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
                con = get_con()
                con.execute(
                    "UPDATE webhook_queue SET skipped = TRUE, status = 'enriched', user_context = 'Skipped', rule_category = NULL, rule_subcategory = NULL, enriched_at = ? WHERE id = ?",
                    [time.strftime("%Y-%m-%d %H:%M:%S"), transaction_id]
                )
                log.info(f"Transaction skipped via Telegram: {transaction_id}")
            except Exception as e:
                log.error(f"Failed to skip transaction {transaction_id}: {e}", exc_info=True)
                bot.send_message(chat_id, "Something went wrong. Try again.")
                return {"ok": True}
            bot.send_message(chat_id, "⏭ Skipped.")

        elif cq.data and cq.data.startswith("taxprop:"):
            # The server only records the decision. Creating the category and
            # moving transactions happens locally, on the next pipeline run,
            # because the taxonomy and the transactions live in the local DB.
            _, decision, local_id = cq.data.split(":", 2)
            try:
                con = get_con()
                row = con.execute(
                    "SELECT proposed_sub, status FROM taxonomy_proposals WHERE local_id = ?",
                    [int(local_id)]
                ).fetchone()
                if not row:
                    bot.send_message(chat_id, "That suggestion has expired.")
                    return {"ok": True}
                proposed_sub, current = row
                if current != "pending":
                    bot.send_message(chat_id, f"Already {current}: <b>{proposed_sub}</b>")
                    return {"ok": True}
                con.execute(
                    "UPDATE taxonomy_proposals SET status = ?, decided_at = ? WHERE local_id = ?",
                    ["approved" if decision == "approve" else "denied",
                     time.strftime("%Y-%m-%d %H:%M:%S"), int(local_id)]
                )
            except Exception as e:
                log.error(f"Failed to record taxonomy decision {local_id}: {e}", exc_info=True)
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
            log.info(f"Taxonomy proposal {local_id} ({proposed_sub}) {decision}d via Telegram")

        elif cq.data and cq.data.startswith("catprop:"):
            # catprop:select:<local_id>:<option_index>, catprop:regenerate:<local_id>,
            # or catprop:denyall:<local_id>. The server only records the decision --
            # everything else (creating a category, asking Claude for fresh options)
            # happens locally on the next run.
            parts = cq.data.split(":")
            action = parts[1]
            local_id = int(parts[2])
            option_index = int(parts[3]) if action == "select" else None
            try:
                con = get_con()
                row = con.execute(
                    "SELECT options, status FROM category_proposals WHERE local_id = ?",
                    [local_id]
                ).fetchone()
                if not row:
                    bot.send_message(chat_id, "That suggestion has expired.")
                    return {"ok": True}
                options, current = json.loads(row[0]), row[1]
                if current != "pending":
                    bot.send_message(chat_id, f"Already {current}.")
                    return {"ok": True}
                if action == "select":
                    con.execute(
                        "UPDATE category_proposals SET status = 'selected', selected_option = ?, decided_at = ? WHERE local_id = ?",
                        [option_index, time.strftime("%Y-%m-%d %H:%M:%S"), local_id]
                    )
                elif action == "regenerate":
                    # Recorded as "denied" under the hood -- see collect_decisions()
                    # locally -- but regenerate_requested keeps these options off
                    # the permanent denied-name blocklist, since asking for
                    # something different isn't a verdict that these were wrong.
                    con.execute(
                        "UPDATE category_proposals SET status = 'denied', regenerate_requested = TRUE, decided_at = ? WHERE local_id = ?",
                        [time.strftime("%Y-%m-%d %H:%M:%S"), local_id]
                    )
                else:
                    con.execute(
                        "UPDATE category_proposals SET status = 'denied', decided_at = ? WHERE local_id = ?",
                        [time.strftime("%Y-%m-%d %H:%M:%S"), local_id]
                    )
            except Exception as e:
                log.error(f"Failed to record category decision {local_id}: {e}", exc_info=True)
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
                bot.send_message(
                    chat_id,
                    "🔄 Got it — you'll get a fresh set of options on the next run."
                )
            else:
                bot.send_message(
                    chat_id,
                    "❌ None of these. Those transaction(s) will be reconsidered on the next run with "
                    "these ideas ruled out."
                )
            log.info(
                f"Category proposal {local_id} {action}"
                + (f" (option {option_index})" if action == "select" else "") + " via Telegram"
            )

        elif cq.data and cq.data.startswith("rulesug:"):
            _, action, sid = cq.data.split(":", 2)
            suggestion_id = int(sid)
            try:
                con = get_con()
                row = con.execute(
                    "SELECT match_field, match_value, auto_context, status, category, subcategory FROM rule_suggestions WHERE id = ?",
                    [suggestion_id]
                ).fetchone()
                if not row:
                    bot.send_message(chat_id, "That suggestion no longer exists.")
                    return {"ok": True}
                match_field, match_value, auto_context, current, category, subcategory = row
                if current != "pending":
                    bot.send_message(chat_id, f"Already {current}.")
                    return {"ok": True}
                if action == "approve":
                    # The placement is fixed, not left to the classifier: the
                    # pattern was never once categorized differently, and was
                    # judged single-purpose before being suggested at all.
                    next_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM rules").fetchone()[0]
                    con.execute(
                        "INSERT INTO rules (id, name, match_field, match_type, match_value, auto_context, category, subcategory) "
                        "VALUES (?, ?, ?, 'exact', ?, ?, ?, ?)",
                        [next_id, f"Suggested: {match_value}", match_field, match_value, auto_context, category, subcategory]
                    )
                    con.execute(
                        "UPDATE rule_suggestions SET status = 'approved', decided_at = ? WHERE id = ?",
                        [time.strftime("%Y-%m-%d %H:%M:%S"), suggestion_id]
                    )
                else:
                    con.execute(
                        "UPDATE rule_suggestions SET status = 'denied', decided_at = ? WHERE id = ?",
                        [time.strftime("%Y-%m-%d %H:%M:%S"), suggestion_id]
                    )
            except Exception as e:
                log.error(f"Failed to record rule suggestion decision {suggestion_id}: {e}", exc_info=True)
                bot.send_message(chat_id, "Something went wrong. Try again.")
                return {"ok": True}
            if action == "approve":
                bot.send_message(
                    chat_id,
                    f"✅ Rule created — <b>{html.escape(match_value)}</b> will go straight to "
                    f"<b>{html.escape(category)} › {html.escape(subcategory)}</b> from now on."
                )
            else:
                bot.send_message(chat_id, "👍 Won't suggest that one again.")
            log.info(f"Rule suggestion {suggestion_id} {action}d via Telegram")

        elif cq.data and cq.data.startswith("quickcat:"):
            _, transaction_id, quick_id = cq.data.split(":", 2)
            if chat_id in _sessions:
                _sessions[chat_id].get("timeout_task") and _sessions[chat_id]["timeout_task"].cancel()
                del _sessions[chat_id]
            try:
                con = get_con()
                row = con.execute(
                    "SELECT category, subcategory FROM quick_categories WHERE id = ?", [int(quick_id)]
                ).fetchone()
                if not row:
                    bot.send_message(chat_id, "That quick category has expired. Tap ✏️ Enrich instead.")
                    return {"ok": True}
                category, subcategory = row
                context = f"{category} - {subcategory}"
                status_row = con.execute("SELECT status FROM webhook_queue WHERE id = ?", [transaction_id]).fetchone()
                was_pending = bool(status_row) and status_row[0] == "pending"
                con.execute(
                    "UPDATE webhook_queue SET user_context = ?, status = 'enriched', rule_category = NULL, rule_subcategory = NULL, enriched_at = ? WHERE id = ?",
                    [context, time.strftime("%Y-%m-%d %H:%M:%S"), transaction_id]
                )
                if was_pending:
                    con.execute("UPDATE stats SET total_enriched = total_enriched + 1 WHERE id = 1")
                log.info(f"Transaction quick-categorized via Telegram: {transaction_id} -> {context}")
            except Exception as e:
                log.error(f"Failed to quick-categorize transaction {transaction_id}: {e}", exc_info=True)
                bot.send_message(chat_id, "Something went wrong. Try again.")
                return {"ok": True}
            bot.send_message(chat_id, f"✅ Saved as {category} → {subcategory}")

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
            con = get_con()
            row = con.execute("SELECT status FROM webhook_queue WHERE id = ?", [transaction_id]).fetchone()
            was_pending = bool(row) and row[0] == "pending"
            con.execute(
                "UPDATE webhook_queue SET user_context = ?, status = 'enriched', rule_category = NULL, rule_subcategory = NULL, enriched_at = ? WHERE id = ?",
                [context, time.strftime("%Y-%m-%d %H:%M:%S"), transaction_id]
            )
            if was_pending:
                con.execute("UPDATE stats SET total_enriched = total_enriched + 1 WHERE id = 1")
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




@app.get("/dashboard/transaction/{transaction_id}", response_class=HTMLResponse)
async def transaction_detail(transaction_id: str, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    con = get_con()
    row = con.execute(
        "SELECT id, payload, received_at, status, user_context, enriched_at, skipped FROM webhook_queue WHERE id = ?",
        [transaction_id]
    ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Transaction not found")

    payload = json.loads(row[1])
    data = payload.get("data", {})
    amount_pence = data.get("amount", 0)
    amount_str = f"-£{abs(amount_pence) / 100:.2f}" if amount_pence < 0 else f"+£{amount_pence / 100:.2f}"

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
            "amount": amount_str,
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


@app.post("/dashboard/transaction/{transaction_id}/skip", response_class=HTMLResponse)
async def skip_transaction(transaction_id: str, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    con = get_con()
    con.execute(
        "UPDATE webhook_queue SET skipped = TRUE, status = 'enriched', user_context = 'Skipped', rule_category = NULL, rule_subcategory = NULL, enriched_at = ? WHERE id = ?",
        [time.strftime("%Y-%m-%d %H:%M:%S"), transaction_id]
    )
    return RedirectResponse(url=f"/dashboard/transaction/{transaction_id}", status_code=303)


@app.post("/dashboard/transaction/{transaction_id}/enrich-dashboard", response_class=HTMLResponse)
async def enrich_transaction_dashboard(transaction_id: str, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    form = await request.form()
    context = (form.get("context") or "").strip()
    if not context:
        return RedirectResponse(url="/dashboard", status_code=303)
    con = get_con()
    row = con.execute("SELECT status FROM webhook_queue WHERE id = ?", [transaction_id]).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Transaction not found")
    was_pending = row[0] == "pending"
    con.execute(
        "UPDATE webhook_queue SET user_context = ?, status = 'enriched', rule_category = NULL, rule_subcategory = NULL, enriched_at = ? WHERE id = ?",
        [context, time.strftime("%Y-%m-%d %H:%M:%S"), transaction_id]
    )
    if was_pending:
        con.execute("UPDATE stats SET total_enriched = total_enriched + 1 WHERE id = 1")
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/dashboard/transaction/{transaction_id}/delete", response_class=HTMLResponse)
async def delete_transaction(transaction_id: str, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    con = get_con()
    con.execute("DELETE FROM webhook_queue WHERE id = ?", [transaction_id])
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/dashboard/transaction/{transaction_id}/edit-context", response_class=HTMLResponse)
async def edit_transaction_context(transaction_id: str, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    form = await request.form()
    context = (form.get("context") or "").strip()
    if context:
        get_con().execute(
            "UPDATE webhook_queue SET user_context = ?, rule_category = NULL, rule_subcategory = NULL, enriched_at = ? WHERE id = ?",
            [context, time.strftime("%Y-%m-%d %H:%M:%S"), transaction_id]
        )
    return RedirectResponse(url=f"/dashboard/transaction/{transaction_id}", status_code=303)


@app.post("/dashboard/transaction/{transaction_id}/reset", response_class=HTMLResponse)
async def reset_transaction(transaction_id: str, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    con = get_con()
    con.execute(
        "UPDATE webhook_queue SET status = 'pending', user_context = NULL, rule_category = NULL, rule_subcategory = NULL, enriched_at = NULL, skipped = FALSE WHERE id = ?",
        [transaction_id]
    )
    return RedirectResponse(url=f"/dashboard/transaction/{transaction_id}", status_code=303)

@app.post("/dashboard/rules/test", response_class=HTMLResponse)
async def test_rules(request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    form = await request.form()
    con = get_con()
    context = {"rules": _get_rules(con), "transactions": _get_test_transactions(con),
               "taxonomy": _get_taxonomy(con), "active_nav": "rules"}

    # "Test this rule" on the Add Rule form: run the not-yet-saved rule
    # against everything sitting in the queue (up to ~7 days of history
    # before cleanup), so you see which real transactions it would catch --
    # not whether it matches a single transaction you already picked it from.
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
        queue = _get_test_transactions(con, limit=None)
        matches = [t for t in queue if _evaluate_rule(t, **candidate)]
        context["candidate_results"] = {
            "rule": {**candidate,
                     "name": (form.get("candidate_name") or "").strip(),
                     "auto_context": (form.get("candidate_auto_context") or "").strip(),
                     "auto_skip": (form.get("candidate_auto_skip") or "") == "on",
                     "category": (form.get("candidate_category") or "").strip(),
                     "subcategory": (form.get("candidate_subcategory") or "").strip()},
            "queue_size": len(queue),
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


@app.post("/dashboard/rules/{rule_id}/edit", response_class=HTMLResponse)
async def edit_rule(rule_id: int, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    match_field = (form.get("match_field") or "").strip()
    match_type = (form.get("match_type") or "").strip()
    match_value = (form.get("match_value") or "").strip()
    auto_context = (form.get("auto_context") or "").strip()
    match_field_2 = (form.get("match_field_2") or "").strip() or None
    match_type_2 = (form.get("match_type_2") or "").strip() or None
    match_value_2 = (form.get("match_value_2") or "").strip() or None
    auto_skip = form.get("auto_skip") == "on"
    category, subcategory = _rule_placement(form)
    if name and match_field and match_type and match_value and (auto_context or auto_skip or category):
        con = get_con()
        con.execute(
            "UPDATE rules SET name=?, match_field=?, match_type=?, match_value=?, auto_context=?, match_field_2=?, match_type_2=?, match_value_2=?, auto_skip=?, category=?, subcategory=? WHERE id=?",
            [name, match_field, match_type, match_value, auto_context or "", match_field_2, match_type_2, match_value_2, auto_skip, category, subcategory, rule_id]
        )
    return RedirectResponse(url="/dashboard/rules", status_code=303)


@app.post("/dashboard/rules/{rule_id}/delete", response_class=HTMLResponse)
async def delete_rule(rule_id: int, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    con = get_con()
    con.execute("DELETE FROM rules WHERE id = ?", [rule_id])
    return RedirectResponse(url="/dashboard/rules", status_code=303)

@app.post("/dashboard/rules/add", response_class=HTMLResponse)
async def add_rule(request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    match_field = (form.get("match_field") or "").strip()
    match_type = (form.get("match_type") or "").strip()
    match_value = (form.get("match_value") or "").strip()
    auto_context = (form.get("auto_context") or "").strip()
    match_field_2 = (form.get("match_field_2") or "").strip() or None
    match_type_2 = (form.get("match_type_2") or "").strip() or None
    match_value_2 = (form.get("match_value_2") or "").strip() or None
    auto_skip = form.get("auto_skip") == "on"
    category, subcategory = _rule_placement(form)
    if name and match_field and match_type and match_value and (auto_context or auto_skip or category):
        con = get_con()
        next_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM rules").fetchone()[0]
        con.execute(
            "INSERT INTO rules (id, name, match_field, match_type, match_value, auto_context, match_field_2, match_type_2, match_value_2, auto_skip, category, subcategory) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [next_id, name, match_field, match_type, match_value, auto_context or "", match_field_2, match_type_2, match_value_2, auto_skip, category, subcategory]
        )
    return RedirectResponse(url="/dashboard/rules", status_code=303)


def _rule_placement(form) -> tuple[str | None, str | None]:
    """A fixed (category, subcategory) from the rule form, or (None, None).
    Only a complete pair counts -- a category picked without a subcategory
    would file transactions under a parent with nothing beneath it, so a
    half-filled pair is treated as no placement at all."""
    category = (form.get("category") or "").strip()
    subcategory = (form.get("subcategory") or "").strip()
    if category and subcategory:
        return category, subcategory
    return None, None


def _get_taxonomy(con) -> dict[str, list[str]]:
    """{category: [subcategory, ...]} from the last pipeline sync, for the
    Rules page's placement dropdowns."""
    taxonomy: dict[str, list[str]] = {}
    for category, subcategory in con.execute(
        "SELECT category, subcategory FROM taxonomy ORDER BY category, subcategory"
    ).fetchall():
        taxonomy.setdefault(category, []).append(subcategory)
    return taxonomy


def _get_rules(con):
    rows = con.execute(
        "SELECT id, name, match_field, match_type, match_value, auto_context, enabled, "
        "match_field_2, match_type_2, match_value_2, auto_skip, category, subcategory FROM rules ORDER BY id"
    ).fetchall()
    return [
        {"id": r[0], "name": r[1], "match_field": r[2], "match_type": r[3],
         "match_value": r[4], "auto_context": r[5], "enabled": r[6],
         "match_field_2": r[7], "match_type_2": r[8], "match_value_2": r[9], "auto_skip": r[10],
         "category": r[11], "subcategory": r[12]}
        for r in rows
    ]


def _txn_short_label(con, transaction_id: str) -> str:
    """One line identifying a transaction in a chat message -- merchant (or
    counterparty, or description, whichever is actually populated) plus the
    signed amount, so "Enriching tx_0000..." doesn't leave the user guessing
    which of several pending transactions they just tapped."""
    row = con.execute("SELECT payload FROM webhook_queue WHERE id = ?", [transaction_id]).fetchone()
    if not row:
        return transaction_id
    data = json.loads(row[0]).get("data", {})
    amount_pence = data.get("amount", 0)
    merchant = (data.get("merchant") or {}).get("name") or ""
    counterparty = (data.get("counterparty") or {}).get("name") or ""
    description = data.get("description") or ""
    display = merchant or counterparty or description or transaction_id
    sign = "-" if amount_pence < 0 else "+"
    return f"{display} · {sign}£{abs(amount_pence) / 100:.2f}"


def _get_test_transactions(con, limit: int | None = 100):
    sql = "SELECT id, payload, received_at, user_context, status FROM webhook_queue ORDER BY received_at DESC"
    rows = con.execute(sql + (f" LIMIT {int(limit)}" if limit else "")).fetchall()
    txns = []
    for row in rows:
        data = json.loads(row[1]).get("data", {})
        amount_pence = data.get("amount", 0)
        merchant = (data.get("merchant") or {}).get("name") or ""
        counterparty = (data.get("counterparty") or {}).get("name") or ""
        description = data.get("description") or ""
        display = merchant or counterparty or description or row[0]
        sign = "-" if amount_pence < 0 else "+"
        amount_str = f"{sign}£{abs(amount_pence) / 100:.2f}"
        date_str = str(row[2])[:10]
        txns.append({
            "id": row[0],
            "label": f"{display} · {amount_str} · {date_str}",
            "merchant_name": merchant,
            "description": description,
            "counterparty_name": counterparty,
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
    Shared by the saved-rule tester and the queue preview for a new rule, so
    both judge a match exactly the same way."""
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
    con = get_con()
    con.execute("UPDATE rules SET enabled = NOT enabled WHERE id = ?", [rule_id])
    return RedirectResponse(url="/dashboard/rules", status_code=303)


DB_PAGE_SIZE = 50

@app.get("/dashboard/db", response_class=HTMLResponse)
async def db_view(request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials),
                  queue_page: int = Query(1, ge=1)):
    con = get_con()
    stats_rows = con.execute("SELECT * FROM stats").fetchall()
    stats_cols = ["id", "total_received", "total_amount_pence", "requests_sent", "total_enriched", "total_processed"]

    queue_total = con.execute("SELECT COUNT(*) FROM webhook_queue").fetchone()[0]
    queue_rows = con.execute(
        "SELECT id, payload, received_at, status, user_context, enriched_at, request_count FROM webhook_queue ORDER BY received_at DESC LIMIT ? OFFSET ?",
        [DB_PAGE_SIZE, (queue_page - 1) * DB_PAGE_SIZE]
    ).fetchall()
    queue_cols = ["id", "payload", "received_at", "status", "user_context", "enriched_at", "request_count"]
    queue_total_pages = max(1, -(-queue_total // DB_PAGE_SIZE))

    rules_rows = con.execute(
        "SELECT id, name, match_field, match_type, match_value, auto_context, enabled, match_field_2, match_type_2, match_value_2, auto_skip FROM rules ORDER BY id"
    ).fetchall()
    rules_cols = ["id", "name", "match_field", "match_type", "match_value", "auto_context", "enabled", "match_field_2", "match_type_2", "match_value_2", "auto_skip"]

    quick_categories_rows = con.execute(
        "SELECT id, category, subcategory, merchant_name, rank FROM quick_categories ORDER BY merchant_name, rank"
    ).fetchall()
    quick_categories_cols = ["id", "category", "subcategory", "merchant_name", "rank"]

    return templates.TemplateResponse(
        request=request,
        name="db.html",
        context={
            "tables": [
                {"name": "stats", "columns": stats_cols, "rows": stats_rows, "total": len(stats_rows), "page": 1, "total_pages": 1, "page_param": None},
                {"name": "webhook_queue", "columns": queue_cols, "rows": queue_rows, "total": queue_total, "page": queue_page, "total_pages": queue_total_pages, "page_param": "queue_page"},
                {"name": "rules", "columns": rules_cols, "rows": rules_rows, "total": len(rules_rows), "page": 1, "total_pages": 1, "page_param": None},
                {"name": "quick_categories", "columns": quick_categories_cols, "rows": quick_categories_rows, "total": len(quick_categories_rows), "page": 1, "total_pages": 1, "page_param": None},
            ],
            "active_nav": "db",
        }
    )


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
    lifetime = con.execute(
        "SELECT total_received, total_amount_pence, requests_sent, total_enriched, total_processed FROM stats WHERE id = 1"
    ).fetchone()
    queue_stats = con.execute(
        "SELECT status, COUNT(*) FROM webhook_queue WHERE status != 'processed' GROUP BY status"
    ).fetchall()

    where_clauses = ["status != 'processed'"]
    params: list = []
    if status_filter == "skipped":
        where_clauses.append("skipped = TRUE")
    elif status_filter in ("pending", "enriched"):
        where_clauses.append("status = ?")
        params.append(status_filter)
    if search:
        where_clauses.append("(id LIKE ? OR payload LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])

    where_sql = " AND ".join(where_clauses)
    total_queue = con.execute(f"SELECT COUNT(*) FROM webhook_queue WHERE {where_sql}", params).fetchone()[0]
    rows = con.execute(
        f"SELECT id, received_at, status, request_count, payload, skipped FROM webhook_queue WHERE {where_sql} ORDER BY received_at DESC LIMIT ? OFFSET ?",
        params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]
    ).fetchall()

    queue = []
    for row in rows:
        amount_pence = json.loads(row[4]).get("data", {}).get("amount", 0)
        data = json.loads(row[4]).get("data", {})
        queue.append({
            "id": row[0],
            "received_at": row[1],
            "status": row[2],
            "request_count": row[3] or 0,
            "amount": f"-£{abs(amount_pence) / 100:.2f}" if amount_pence < 0 else f"+£{amount_pence / 100:.2f}",
            "is_debit": amount_pence < 0,
            "skipped": bool(row[5]),
            "description": data.get("description", ""),
            "merchant": (data.get("merchant") or {}).get("name", "") or data.get("counterparty", {}).get("name", ""),
        })

    total_received, total_amount_pence, requests_sent, total_enriched, total_processed = lifetime or (0, 0, 0, 0, 0)
    total_pages = max(1, -(-total_queue // PAGE_SIZE))

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "total_received": total_received,
            "total_amount": f"£{abs(total_amount_pence) / 100:,.2f}",
            "requests_sent": requests_sent,
            "total_enriched": total_enriched,
            "total_processed": total_processed,
            "queue_stats": queue_stats,
            "queue": queue,
            "page": page,
            "total_pages": total_pages,
            "total_queue": total_queue,
            "status_filter": status_filter,
            "search": search,
            "active_nav": "dashboard",
        }
    )


@app.post("/dashboard/bulk/delete-skipped", response_class=HTMLResponse)
async def bulk_delete_skipped(request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    get_con().execute("DELETE FROM webhook_queue WHERE skipped = TRUE")
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/dashboard/bulk/requeue-pending", response_class=HTMLResponse)
async def bulk_requeue_pending(request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    get_con().execute(
        "UPDATE webhook_queue SET status = 'pending', user_context = NULL, rule_category = NULL, rule_subcategory = NULL, enriched_at = NULL, skipped = FALSE WHERE status = 'pending'"
    )
    return RedirectResponse(url="/dashboard", status_code=303)


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


@app.get('/export')
async def export(api_key: str = Security(API_KEY_HEADER)):
    await verify_api_key(api_key)
    con = get_con()
    rows = con.execute(
        """SELECT id, payload, received_at, user_context, enriched_at, skipped, rule_category, rule_subcategory
           FROM webhook_queue
           WHERE status = 'enriched'
           ORDER BY enriched_at ASC"""
    ).fetchall()
    return [
        {
            "id": row[0],
            "payload": json.loads(row[1]),
            "received_at": str(row[2]),
            "user_context": row[3],
            "enriched_at": str(row[4]),
            "skipped": row[5],
            "rule_category": row[6],
            "rule_subcategory": row[7],
        }
        for row in rows
    ]

@app.post('/mark-processed')
async def mark_processed(body: MarkProcessedRequest, api_key: str = Security(API_KEY_HEADER)):
    await verify_api_key(api_key)
    con = get_con()
    placeholders = ", ".join("?" * len(body.ids))
    con.execute(
        f"UPDATE webhook_queue SET status = 'processed' WHERE id IN ({placeholders})",
        body.ids
    )
    con.execute(
        "UPDATE stats SET total_processed = total_processed + ? WHERE id = 1",
        [len(body.ids)]
    )
    log.info(f"Marked {len(body.ids)} transactions as processed")
    return {"marked": len(body.ids)}


@app.post('/sync-quick-categories')
async def sync_quick_categories(body: SyncQuickCategoriesRequest, api_key: str = Security(API_KEY_HEADER)):
    await verify_api_key(api_key)
    con = get_con()
    con.execute("DELETE FROM quick_categories")
    for i, entry in enumerate(body.entries):
        con.execute(
            "INSERT INTO quick_categories (id, category, subcategory, merchant_name, rank) VALUES (?, ?, ?, ?, ?)",
            [i, entry.category, entry.subcategory, entry.merchant_name, entry.rank]
        )
    log.info(f"Synced {len(body.entries)} quick categories")
    return {"synced": len(body.entries)}


@app.post('/sync-taxonomy')
async def sync_taxonomy(body: SyncTaxonomyRequest, api_key: str = Security(API_KEY_HEADER)):
    """Replace the server's copy of the local taxonomy -- names only, for the
    Rules page's placement dropdowns. Replaced wholesale each run, so a
    renamed or deleted category stops being offered on the next sync."""
    await verify_api_key(api_key)
    con = get_con()
    con.execute("DELETE FROM taxonomy")
    pairs = {(e.category, e.subcategory) for e in body.entries}
    for category, subcategory in pairs:
        con.execute("INSERT INTO taxonomy (category, subcategory) VALUES (?, ?)", [category, subcategory])
    log.info(f"Synced {len(pairs)} taxonomy entries")
    return {"synced": len(pairs)}


@app.post('/sync-taxonomy-proposals')
async def sync_taxonomy_proposals(body: SyncTaxonomyProposalsRequest, api_key: str = Security(API_KEY_HEADER)):
    """Store the month's proposals and send the cards. Existing undecided
    proposals are cleared first: the local side only sends a batch once per
    month, so anything still pending has been superseded by this run."""
    await verify_api_key(api_key)
    con = get_con()
    con.execute("DELETE FROM taxonomy_proposals WHERE status = 'pending'")
    stored = []
    for p in body.proposals:
        con.execute(
            """INSERT INTO taxonomy_proposals
               (local_id, parent_name, source_sub, action, target_parent,
                proposed_sub, rationale, evidence_count, examples, status, sent_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
               ON CONFLICT (local_id) DO NOTHING""",
            [p.id, p.parent_name, p.source_sub, p.action, p.target_parent,
             p.proposed_sub, p.rationale, p.evidence_count,
             json.dumps(p.examples), time.strftime("%Y-%m-%d %H:%M:%S")]
        )
        stored.append(p)

    if stored:
        chat_id = int(os.getenv("TELEGRAM_CHAT_ID"))
        bot.send_taxonomy_intro(chat_id, len(stored))
        for p in stored:
            bot.send_taxonomy_proposal(chat_id, {
                "local_id": p.id, "parent_name": p.parent_name,
                "source_sub": p.source_sub, "action": p.action,
                "target_parent": p.target_parent, "proposed_sub": p.proposed_sub,
                "rationale": p.rationale, "evidence_count": p.evidence_count,
                "examples": p.examples,
            })
    log.info(f"Received {len(stored)} taxonomy proposal(s) and sent cards")
    return {"received": len(stored)}


@app.get('/taxonomy-decisions')
async def taxonomy_decisions(api_key: str = Security(API_KEY_HEADER)):
    """Decisions the local pipeline hasn't collected yet. Marked 'collected'
    only once the local side confirms via /taxonomy-decisions/collected, so a
    failed local run doesn't lose an approval."""
    await verify_api_key(api_key)
    rows = get_con().execute(
        """SELECT local_id, proposed_sub, status FROM taxonomy_proposals
           WHERE status IN ('approved', 'denied') ORDER BY local_id"""
    ).fetchall()
    return {"decisions": [{"id": r[0], "proposed_sub": r[1], "status": r[2]} for r in rows]}


@app.post('/taxonomy-decisions/collected')
async def taxonomy_decisions_collected(body: MarkProcessedRequest, api_key: str = Security(API_KEY_HEADER)):
    await verify_api_key(api_key)
    con = get_con()
    for local_id in body.ids:
        con.execute(
            "UPDATE taxonomy_proposals SET status = 'collected' WHERE local_id = ?",
            [int(local_id)]
        )
    return {"collected": len(body.ids)}


@app.post('/send-alert')
async def send_alert(body: AlertRequest, api_key: str = Security(API_KEY_HEADER)):
    """Let the local pipeline reach Telegram, which only the server can. Used
    for "I can't do my job" notices -- out of Anthropic credit, a rejected key --
    that would otherwise sit unseen in a log file on the local machine. Text
    only; the local side never sends transaction data through this."""
    await verify_api_key(api_key)
    bot.send_alert(int(os.getenv("TELEGRAM_CHAT_ID")), body.title, body.message)
    log.info(f"Sent alert to Telegram: {body.title}")
    return {"sent": True}


@app.post('/sync-category-proposals')
async def sync_category_proposals(body: SyncCategoryProposalsRequest, api_key: str = Security(API_KEY_HEADER)):
    """Store newly created real-time category proposals and send their cards.
    Unlike /sync-taxonomy-proposals, nothing pending is cleared first -- these
    arrive continuously (several pipeline runs a day), not one batch a month.

    Checks for a pre-existing row before inserting, rather than INSERT ...
    ON CONFLICT DO NOTHING: that let a local_id collision (any row already on
    the server for that id, in any status) silently no-op the insert while
    still sending a card built from the NEW proposal's content -- a card that
    then didn't match what was actually stored, so tapping it just replied
    "Already <old status>." A collision is unlikely in normal operation (ids
    only ever increase), but can happen if a local database is restored from
    a backup older than the server's own state, so it's worth guarding against
    outright rather than trusting it can't occur."""
    await verify_api_key(api_key)
    con = get_con()
    stored = []
    for p in body.proposals:
        if con.execute("SELECT 1 FROM category_proposals WHERE local_id = ?", [p.id]).fetchone():
            log.warning(f"Category proposal local_id={p.id} already exists on the server -- skipping, not sending a card for it")
            continue
        con.execute(
            """INSERT INTO category_proposals
               (local_id, options, txn_count, examples, status, sent_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            [p.id, json.dumps([o.model_dump() for o in p.options]), p.txn_count,
             json.dumps(p.examples), time.strftime("%Y-%m-%d %H:%M:%S")]
        )
        stored.append(p)

    if stored:
        chat_id = int(os.getenv("TELEGRAM_CHAT_ID"))
        for p in stored:
            bot.send_category_proposal(chat_id, {
                "local_id": p.id, "options": [o.model_dump() for o in p.options],
                "txn_count": p.txn_count, "examples": p.examples,
            })
    log.info(f"Received {len(stored)} category proposal(s) and sent cards")
    return {"received": len(stored)}


def _rule_suggestion_already_covered(con, match_field: str, match_value: str) -> bool:
    """Whether an existing ENABLED rule already catches this pattern's
    identifying value on its own. A rule with a second condition can't be
    verified without a full transaction to test it against, so it's treated
    as not covering this -- erring toward still suggesting rather than
    silently missing a genuinely useful one."""
    from check_rules import _matches
    rows = con.execute(
        """SELECT match_type, match_value FROM rules
           WHERE enabled = TRUE AND match_field = ?
           AND (match_field_2 IS NULL OR match_field_2 = '')""",
        [match_field]
    ).fetchall()
    for match_type, existing_value in rows:
        try:
            if _matches(match_value, match_type, existing_value):
                return True
        except Exception:
            continue
    return False


@app.post('/sync-rule-suggestions')
async def sync_rule_suggestions(body: SyncRuleSuggestionsRequest, api_key: str = Security(API_KEY_HEADER)):
    """Store genuinely new rule suggestions and send their cards. Skips a
    candidate already asked about -- in any status, so a denied one stays
    denied -- and one an existing enabled rule already covers, since neither
    needs asking about again."""
    await verify_api_key(api_key)
    con = get_con()
    stored = []
    for c in body.candidates:
        if con.execute(
            "SELECT 1 FROM rule_suggestions WHERE match_field = ? AND match_value = ?",
            [c.match_field, c.match_value]
        ).fetchone():
            continue
        if _rule_suggestion_already_covered(con, c.match_field, c.match_value):
            continue
        next_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM rule_suggestions").fetchone()[0]
        con.execute(
            """INSERT INTO rule_suggestions
               (id, match_field, match_value, category, subcategory, txn_count, auto_context, status, proposed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            [next_id, c.match_field, c.match_value, c.category, c.subcategory, c.txn_count, c.auto_context,
             time.strftime("%Y-%m-%d %H:%M:%S")]
        )
        stored.append({"id": next_id, **c.model_dump()})

    if stored:
        chat_id = int(os.getenv("TELEGRAM_CHAT_ID"))
        for s in stored:
            bot.send_rule_suggestion(chat_id, s)
    log.info(f"Received {len(stored)} rule suggestion(s) and sent cards")
    return {"received": len(stored)}


@app.get('/category-decisions')
async def category_decisions(api_key: str = Security(API_KEY_HEADER)):
    """Decisions the local pipeline hasn't collected yet. Marked 'collected'
    only once the local side confirms via /category-decisions/collected, so a
    failed local run doesn't lose an approval."""
    await verify_api_key(api_key)
    rows = get_con().execute(
        """SELECT local_id, status, selected_option, regenerate_requested FROM category_proposals
           WHERE status IN ('selected', 'denied') ORDER BY local_id"""
    ).fetchall()
    return {"decisions": [
        {"id": r[0], "status": r[1], "selected_option": r[2], "regenerate_requested": bool(r[3])}
        for r in rows
    ]}


@app.post('/category-decisions/collected')
async def category_decisions_collected(body: MarkProcessedRequest, api_key: str = Security(API_KEY_HEADER)):
    await verify_api_key(api_key)
    con = get_con()
    for local_id in body.ids:
        con.execute(
            "UPDATE category_proposals SET status = 'collected' WHERE local_id = ?",
            [int(local_id)]
        )
    return {"collected": len(body.ids)}


@app.post('/monthly-report')
async def monthly_report(body: MonthlyReportRequest, api_key: str = Security(API_KEY_HEADER)):
    await verify_api_key(api_key)
    bot = TelegramBot()
    bot.send_message(int(os.getenv("TELEGRAM_CHAT_ID")), body.message)
    return {"ok": True}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
