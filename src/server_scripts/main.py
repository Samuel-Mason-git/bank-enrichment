from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn
import logging
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


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
