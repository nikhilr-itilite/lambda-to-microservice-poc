from pydantic import BaseModel, Field
from typing import Optional


class HotelVendorRequestPayload(BaseModel):
    check_in_date: str
    checkout_date: str
    latitude: str
    longitude: str
    city: str
    country: str
    no_of_rooms: int = Field(default=1)
    no_of_adults: int = Field(default=1)
    no_of_children: int = Field(default=0)
    radius: int = Field(default=20)
    currency: str = Field(default="INR")
    hotel_ids: Optional[str]
