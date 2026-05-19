from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, HTTPException, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional
import uvicorn
import logging
import secrets
import time
import json
import os

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from server_db import init_db, get_con
from telegram import TelegramBot

BASE_DIR = os.path.dirname(__file__)

LOG_PATH = os.path.join(BASE_DIR, "..", "..", "data", "server.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=2),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

DASHBOARD_USER = os.getenv("DASHBOARD_USER")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD")
_sessions: dict = {}

security = HTTPBasic()
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


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
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


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
    body = await request.body()
    try:
        monzo_data = MonzoOuter.model_validate_json(body)
    except Exception as e:
        log.error(f"Validation error: {e}. Body: {body.decode()}")
        raise HTTPException(status_code=422, detail="Invalid payload")

    received_at = time.strftime("%Y-%m-%d %H:%M:%S")
    transaction_id = monzo_data.data.id

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

    if is_new and bot:
        try:
            bot.send_card(json.loads(body))
            con.execute(
                "UPDATE webhook_queue SET request_count = request_count + 1 WHERE id = ?",
                [transaction_id]
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
            _sessions[chat_id] = {"transaction_id": transaction_id}
            bot.send_message(chat_id, f"Enriching <code>{transaction_id}</code>\n\nWhat was this transaction? Send a one-line description.")

    elif update.message and update.message.text:
        msg = update.message
        chat_id = msg.chat.id
        session = _sessions.get(chat_id)

        if not session:
            return {"ok": True}

        context = msg.text.strip()
        transaction_id = session["transaction_id"]

        try:
            con = get_con()
            con.execute(
                "UPDATE webhook_queue SET user_context = ?, status = 'enriched', enriched_at = ? WHERE id = ?",
                [context, time.strftime("%Y-%m-%d %H:%M:%S"), transaction_id]
            )
            con.execute("UPDATE stats SET total_enriched = total_enriched + 1 WHERE id = 1")
            log.info(f"Transaction enriched: {transaction_id}")
        except Exception as e:
            log.error(f"Failed to enrich transaction {transaction_id}: {e}", exc_info=True)
            bot.send_message(chat_id, "Something went wrong saving that. Try again.")
            return {"ok": True}

        del _sessions[chat_id]
        bot.send_message(chat_id, "✅ Saved.")

    return {"ok": True}




@app.get("/dashboard/transaction/{transaction_id}", response_class=HTMLResponse)
async def transaction_detail(transaction_id: str, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    con = get_con()
    row = con.execute(
        "SELECT id, payload, received_at, status, user_context, enriched_at FROM webhook_queue WHERE id = ?",
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


@app.post("/dashboard/transaction/{transaction_id}/reset", response_class=HTMLResponse)
async def reset_transaction(transaction_id: str, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    con = get_con()
    con.execute(
        "UPDATE webhook_queue SET status = 'pending', user_context = NULL, enriched_at = NULL WHERE id = ?",
        [transaction_id]
    )
    return RedirectResponse(url=f"/dashboard/transaction/{transaction_id}", status_code=303)


@app.get("/dashboard/db", response_class=HTMLResponse)
async def db_view(request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    con = get_con()
    stats_rows = con.execute("SELECT * FROM stats").fetchall()
    stats_cols = ["id", "total_received", "total_amount_pence", "requests_sent", "total_enriched", "total_processed"]
    queue_rows = con.execute(
        "SELECT id, payload, received_at, status, user_context, enriched_at, request_count FROM webhook_queue ORDER BY received_at DESC"
    ).fetchall()
    queue_cols = ["id", "payload", "received_at", "status", "user_context", "enriched_at", "request_count"]
    return templates.TemplateResponse(
        request=request,
        name="db.html",
        context={
            "tables": [
                {"name": "stats", "columns": stats_cols, "rows": stats_rows},
                {"name": "webhook_queue", "columns": queue_cols, "rows": queue_rows},
            ]
        }
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    con = get_con()
    lifetime = con.execute(
        "SELECT total_received, total_amount_pence, requests_sent, total_enriched, total_processed FROM stats WHERE id = 1"
    ).fetchone()
    queue_stats = con.execute(
        "SELECT status, COUNT(*) FROM webhook_queue WHERE status != 'processed' GROUP BY status"
    ).fetchall()
    rows = con.execute(
        "SELECT id, received_at, status, request_count, payload FROM webhook_queue WHERE status != 'processed' ORDER BY received_at DESC"
    ).fetchall()

    queue = []
    for row in rows:
        amount_pence = json.loads(row[4]).get("data", {}).get("amount", 0)
        queue.append({
            "id": row[0],
            "received_at": row[1],
            "status": row[2],
            "request_count": row[3] or 0,
            "amount": f"-£{abs(amount_pence) / 100:.2f}" if amount_pence < 0 else f"+£{amount_pence / 100:.2f}",
            "is_debit": amount_pence < 0,
        })

    total_received, total_amount_pence, requests_sent, total_enriched, total_processed = lifetime or (0, 0, 0, 0, 0)
    total_amount_str = f"£{abs(total_amount_pence) / 100:,.2f}"

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "total_received": total_received,
            "total_amount": total_amount_str,
            "requests_sent": requests_sent,
            "total_enriched": total_enriched,
            "total_processed": total_processed,
            "queue_stats": queue_stats,
            "queue": queue,
        }
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
