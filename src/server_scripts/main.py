from contextlib import asynccontextmanager
from fastapi import FastAPI
from pydantic import BaseModel
import uvicorn

from server_db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
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
    print("Payload Recieved")
    payload = monzo_data.model_dump()
    return {
        "msg": "200 ok, data recieved",
        "Package recieved": payload,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
