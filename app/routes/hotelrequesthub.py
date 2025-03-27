from fastapi import APIRouter, HTTPException, status
from app.services.hotelrequesthub import app


router = APIRouter()


@router.get("/")
async def initiate_hotelrequesthub():
    print("*********************HERE*****************************\n\n")
    response = app.handler(None,None)
    print(response)
    if not response:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No data found",
        )
    return response