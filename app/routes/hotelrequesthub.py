from fastapi import APIRouter, HTTPException, status
from app.services.hotelrequesthub import app


router = APIRouter()


@router.get("/")
async def initiate_hotelrequesthub():
    return app.handler(None)