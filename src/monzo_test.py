from dotenv import load_dotenv, dotenv_values
import os
import requests 
import ngrok
from fastapi import FastAPI
import uvicorn
from pydantic import BaseModel

# Loading environment variables
load_dotenv()
config = dotenv_values(".env")

# Set testing token and create webhook listener
ngrok.set_auth_token(os.getenv("NGROK_TOKEN"))
listener = ngrok.connect(8000)
print(f"ngrok tunnel opened at: {listener.url()}")

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
    uvicorn.run(app, host="127.0.0.1", port=8000)