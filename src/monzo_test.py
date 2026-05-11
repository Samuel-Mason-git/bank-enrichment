from dotenv import load_dotenv, dotenv_values
import os
import requests 
from fastapi import FastAPI
import uvicorn
from pydantic import BaseModel

# Loading environment variables
load_dotenv()
config = dotenv_values(".env")

# Create the App
app = FastAPI()

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