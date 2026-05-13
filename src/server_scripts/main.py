from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, HTTPException, Depends, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info("Database initialised")
    yield


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    log.error(f"422 validation error. Body: {body.decode()}")
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


class inner(BaseModel):
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

class outer(BaseModel):
    model_config = ConfigDict(extra='ignore')
    type: str
    data: inner


@app.post('/recieve_monzo/')
async def recieve_monzo(request: Request):
    body = await request.body()
    try:
        monzo_data = outer.model_validate_json(body)
    except Exception as e:
        log.error(f"Validation error: {e}. Body: {body.decode()}")
        raise HTTPException(status_code=422, detail="Invalid payload")

    received_at = time.strftime("%Y-%m-%d %H:%M:%S")
    transaction_id = monzo_data.data.id

    try:
        con = get_con()
        con.execute(
            "INSERT OR IGNORE INTO webhook_queue (id, payload, received_at) VALUES (?, ?, ?)",
            [transaction_id, body.decode(), received_at]
        )
        log.info(f"Transaction stored: {transaction_id}")
    except Exception as e:
        log.error(f"Failed to store transaction {transaction_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to store transaction")

    return {"status": "ok"}


@app.get("/dashboard/transaction/{transaction_id}", response_class=HTMLResponse)
async def transaction_detail(transaction_id: str, request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    con = get_con()
    row = con.execute(
        "SELECT id, payload, received_at, status FROM webhook_queue WHERE id = ?",
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


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    con = get_con()
    total = con.execute("SELECT COUNT(*) FROM webhook_queue").fetchone()[0]
    by_status = con.execute(
        "SELECT status, COUNT(*) FROM webhook_queue GROUP BY status"
    ).fetchall()
    recent = con.execute(
        "SELECT id, received_at, status FROM webhook_queue ORDER BY received_at DESC LIMIT 20"
    ).fetchall()

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "total": total,
            "by_status": by_status,
            "recent": recent,
        }
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
