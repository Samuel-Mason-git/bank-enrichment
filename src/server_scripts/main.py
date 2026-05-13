from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel
import uvicorn
import logging
import secrets
import time
import json
import os

from server_db import init_db, get_con

LOG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "server.log")
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


class inner(BaseModel):
    account_id: str
    amount: int
    created: str
    currency: str
    description: str
    id: str
    category: str
    is_load: bool
    settled: str
    merchant: dict

class outer(BaseModel):
    type: str
    data: inner


@app.post('/recieve_monzo/')
async def recieve_monzo(monzo_data: outer):
    payload = monzo_data.model_dump()
    received_at = time.strftime("%Y-%m-%d %H:%M:%S")
    transaction_id = monzo_data.data.id

    try:
        con = get_con()
        con.execute(
            "INSERT OR IGNORE INTO webhook_queue (id, payload, received_at) VALUES (?, ?, ?)",
            [transaction_id, json.dumps(payload), received_at]
        )
        log.info(f"Transaction stored: {transaction_id}")
    except Exception as e:
        log.error(f"Failed to store transaction {transaction_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to store transaction")

    return {"status": "ok"}


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(credentials: HTTPBasicCredentials = Depends(verify_credentials)):
    con = get_con()
    total = con.execute("SELECT COUNT(*) FROM webhook_queue").fetchone()[0]
    by_status = con.execute(
        "SELECT status, COUNT(*) FROM webhook_queue GROUP BY status"
    ).fetchall()
    recent = con.execute(
        "SELECT id, received_at, status FROM webhook_queue ORDER BY received_at DESC LIMIT 20"
    ).fetchall()

    status_rows = "".join(
        f"<tr><td>{s}</td><td>{c}</td></tr>" for s, c in by_status
    )
    transaction_rows = "".join(
        f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td></tr>" for r in recent
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>Bank Enrichment</title>
    <style>
        body {{ font-family: sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; color: #222; }}
        h1 {{ font-size: 1.4rem; margin-bottom: 24px; }}
        h2 {{ font-size: 1rem; margin-top: 32px; color: #555; }}
        .stat {{ display: inline-block; background: #f4f4f4; border-radius: 8px; padding: 16px 28px; margin-right: 12px; font-size: 1.6rem; font-weight: bold; }}
        .stat span {{ display: block; font-size: 0.75rem; font-weight: normal; color: #888; margin-top: 4px; }}
        table {{ border-collapse: collapse; width: 100%; margin-top: 12px; font-size: 0.9rem; }}
        th {{ text-align: left; border-bottom: 2px solid #ddd; padding: 8px; color: #555; }}
        td {{ padding: 8px; border-bottom: 1px solid #eee; }}
    </style>
</head>
<body>
    <h1>Bank Enrichment Dashboard</h1>
    <div class="stat">{total}<span>Total Transactions</span></div>
    <h2>By Status</h2>
    <table>
        <tr><th>Status</th><th>Count</th></tr>
        {status_rows}
    </table>
    <h2>Recent Transactions</h2>
    <table>
        <tr><th>ID</th><th>Received At</th><th>Status</th></tr>
        {transaction_rows}
    </table>
</body>
</html>"""
    return HTMLResponse(content=html)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
