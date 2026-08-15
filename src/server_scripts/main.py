from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, HTTPException, Depends, Request, status, Query, Security
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials, APIKeyHeader
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional
import asyncio
import uvicorn
import logging
import secrets
import time
import json
import os

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from server_db import init_db, get_con, get_quick_categories
from telegram import TelegramBot
from check_rules import check_rules
from follow_up_tg import requester_loop

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
    task = asyncio.create_task(requester_loop(bot))
    log.info("Requester loop started")
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


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

@app.post('/recieve_monzo/')
async def recieve_monzo(request: Request):
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

    # Check rules for auto-enrichment / auto-skip
    rule_context, rule_skip = (None, False)
    if is_new:
        try:
            rule_context, rule_skip = check_rules(monzo_data.data)
        except Exception as e:
            log.error(f"Rule check failed for {transaction_id}, falling back to no match: {e}", exc_info=True)
    if rule_context or rule_skip:
        try:
            con.execute(
                "UPDATE webhook_queue SET user_context = ?, status = 'enriched', enriched_at = ?, skipped = ? WHERE id = ?",
                [rule_context, time.strftime("%Y-%m-%d %H:%M:%S"), rule_skip, transaction_id]
            )
            con.execute("UPDATE stats SET total_enriched = total_enriched + 1 WHERE id = 1")
            log.info(f"Transaction auto-enriched by rule: {transaction_id} → context='{rule_context}' skip={rule_skip}")
        except Exception as e:
            log.error(f"Failed to auto-enrich transaction {transaction_id}: {e}", exc_info=True)

    # Send Telegram Message
    if is_new and not rule_context and not rule_skip and bot:
        merchant_name = monzo_data.data.merchant.get('name') if monzo_data.data.merchant else None
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



@app.post('/recieve_telegram/')
async def recieve_telegram(request: Request):
    body = await request.body()
    try:
        update = TelegramUpdate.model_validate_json(body)
    except Exception as e:
        log.error(f"Telegram parse error: {e}. Body: {body.decode()}")
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
            bot.send_message(chat_id, f"Enriching <code>{transaction_id}</code>\n\nWhat was this transaction? Send a one-line description.")

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
                    "UPDATE webhook_queue SET skipped = TRUE, status = 'enriched', user_context = 'Skipped', enriched_at = ? WHERE id = ?",
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
                    "UPDATE webhook_queue SET user_context = ?, status = 'enriched', enriched_at = ? WHERE id = ?",
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
                "UPDATE webhook_queue SET user_context = ?, status = 'enriched', enriched_at = ? WHERE id = ?",
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
        "UPDATE webhook_queue SET skipped = TRUE, status = 'enriched', user_context = 'Skipped', enriched_at = ? WHERE id = ?",
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
        "UPDATE webhook_queue SET user_context = ?, status = 'enriched', enriched_at = ? WHERE id = ?",
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
            "UPDATE webhook_queue SET user_context = ?, enriched_at = ? WHERE id = ?",
            [context, time.strftime("%Y-%m-%d %H:%M:%S"), transaction_id]
        )
    return RedirectResponse(url=f"/dashboard/transaction/{transaction_id}", status_code=303)


@app.post("/dashboard/transaction/{transaction_id}/reset", response_class=HTMLResponse)
async def reset_transaction(transaction_id: str, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    con = get_con()
    con.execute(
        "UPDATE webhook_queue SET status = 'pending', user_context = NULL, enriched_at = NULL, skipped = FALSE WHERE id = ?",
        [transaction_id]
    )
    return RedirectResponse(url=f"/dashboard/transaction/{transaction_id}", status_code=303)

@app.post("/dashboard/rules/test", response_class=HTMLResponse)
async def test_rules(request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    from check_rules import _extract_field, _matches
    import re as _re
    form = await request.form()
    test_data = {
        "merchant_name": (form.get("merchant_name") or "").strip(),
        "description": (form.get("description") or "").strip(),
        "counterparty_name": (form.get("counterparty_name") or "").strip(),
        "category": (form.get("category") or "").strip(),
        "scheme": (form.get("scheme") or "").strip(),
        "amount": (form.get("amount") or "0").strip(),
    }
    try:
        amount_pence = int(float(test_data["amount"]) * 100)
    except ValueError:
        amount_pence = 0

    con = get_con()
    rules = con.execute(
        "SELECT id, name, match_field, match_type, match_value, auto_context, enabled, match_field_2, match_type_2, match_value_2, auto_skip FROM rules ORDER BY id"
    ).fetchall()

    results = []
    for rule in rules:
        rule_id, name, match_field, match_type, match_value, auto_context, enabled, match_field_2, match_type_2, match_value_2, auto_skip = rule

        def get_field(field):
            if field == "merchant_name": return test_data["merchant_name"] or None
            if field == "description": return test_data["description"] or None
            if field == "counterparty_name": return test_data["counterparty_name"] or None
            if field == "category": return test_data["category"] or None
            if field == "scheme": return test_data["scheme"] or None
            if field == "amount": return amount_pence
            return None

        try:
            v1 = get_field(match_field)
            cond1 = v1 is not None and _matches(v1, match_type, match_value)
            cond2 = True
            if match_field_2 and match_type_2 and match_value_2:
                v2 = get_field(match_field_2)
                cond2 = v2 is not None and _matches(v2, match_type_2, match_value_2)
            matched = bool(enabled) and cond1 and cond2
        except Exception as e:
            matched = False

        results.append({
            "id": rule_id, "name": name, "enabled": enabled,
            "matched": matched, "auto_context": auto_context, "auto_skip": bool(auto_skip),
        })

    return templates.TemplateResponse(
        request=request,
        name="rules.html",
        context={"rules": _get_rules(con), "transactions": _get_test_transactions(con), "active_nav": "rules", "test_results": results, "test_data": test_data}
    )


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
    if name and match_field and match_type and match_value and (auto_context or auto_skip):
        con = get_con()
        con.execute(
            "UPDATE rules SET name=?, match_field=?, match_type=?, match_value=?, auto_context=?, match_field_2=?, match_type_2=?, match_value_2=?, auto_skip=? WHERE id=?",
            [name, match_field, match_type, match_value, auto_context or "", match_field_2, match_type_2, match_value_2, auto_skip, rule_id]
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
    if name and match_field and match_type and match_value and (auto_context or auto_skip):
        con = get_con()
        next_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM rules").fetchone()[0]
        con.execute(
            "INSERT INTO rules (id, name, match_field, match_type, match_value, auto_context, match_field_2, match_type_2, match_value_2, auto_skip) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [next_id, name, match_field, match_type, match_value, auto_context or "", match_field_2, match_type_2, match_value_2, auto_skip]
        )
    return RedirectResponse(url="/dashboard/rules", status_code=303)


def _get_rules(con):
    rows = con.execute(
        "SELECT id, name, match_field, match_type, match_value, auto_context, enabled, "
        "match_field_2, match_type_2, match_value_2, auto_skip FROM rules ORDER BY id"
    ).fetchall()
    return [
        {"id": r[0], "name": r[1], "match_field": r[2], "match_type": r[3],
         "match_value": r[4], "auto_context": r[5], "enabled": r[6],
         "match_field_2": r[7], "match_type_2": r[8], "match_value_2": r[9], "auto_skip": r[10]}
        for r in rows
    ]


def _get_test_transactions(con):
    rows = con.execute(
        "SELECT id, payload, received_at FROM webhook_queue ORDER BY received_at DESC LIMIT 100"
    ).fetchall()
    txns = []
    for row in rows:
        data = json.loads(row[1]).get("data", {})
        amount_pence = data.get("amount", 0)
        merchant = (data.get("merchant") or {}).get("name") or ""
        counterparty = (data.get("counterparty") or {}).get("name") or ""
        description = data.get("description") or ""
        display = merchant or counterparty or description or row[0]
        amount_str = f"£{abs(amount_pence) / 100:.2f}"
        date_str = str(row[2])[:10]
        txns.append({
            "id": row[0],
            "label": f"{display} · {amount_str} · {date_str}",
            "merchant_name": merchant,
            "description": description,
            "counterparty_name": counterparty,
            "category": data.get("category") or "",
            "scheme": data.get("scheme") or "",
            "amount": round(abs(amount_pence) / 100, 2),
        })
    return txns


@app.get("/dashboard/rules", response_class=HTMLResponse)
async def rules_view(request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    con = get_con()
    return templates.TemplateResponse(
        request=request,
        name="rules.html",
        context={"rules": _get_rules(con), "transactions": _get_test_transactions(con), "active_nav": "rules"}
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
        "UPDATE webhook_queue SET status = 'pending', user_context = NULL, enriched_at = NULL, skipped = FALSE WHERE status = 'pending'"
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
        """SELECT id, payload, received_at, user_context, enriched_at, skipped
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


@app.post('/monthly-report')
async def monthly_report(body: MonthlyReportRequest, api_key: str = Security(API_KEY_HEADER)):
    await verify_api_key(api_key)
    bot = TelegramBot()
    bot.send_message(int(os.getenv("TELEGRAM_CHAT_ID")), body.message)
    return {"ok": True}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
