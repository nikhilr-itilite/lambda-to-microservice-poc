from fastapi import FastAPI
from dotenv import load_dotenv
from app.routes import hotelrequesthub

load_dotenv()


app = FastAPI(
    title="Hotel Packaging POC",
    version="0.1.0"
)

app.include_router(hotelrequesthub.router, prefix="/hotelrequesthub", tags=["hotelrequesthub"])

@app.get("/") 
async def root():
    return {"message": "Hello FastAPI!"}

@app.get("/ping")
async def ping():
    return {"message": "pong"}