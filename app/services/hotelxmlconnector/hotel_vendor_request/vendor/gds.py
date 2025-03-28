import os

import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional

from helperlayer import HotelFREConfig, AES_decryption_data
from app.services.hotelxmlconnector.hotel_vendor_request.hotel import HotelVendorRequestPayload
from app.services.hotelxmlconnector.hotel_vendor_request.SOAP import SOAPManager
from pydantic import BaseModel, Field
from zeep import Settings
from opensearchlogger.logging import logger
from helperlayer import push_newrelic_custom_event, NR_API_EXECUTION_EVENT, HotelAPITypes
import newrelic.agent
from dotenv import load_dotenv

newrelic_agent = newrelic.agent.register_application()

from ..helper import construct_permitted_chains

load_dotenv()

TP_UAPI_VERSION = os.environ["TP_UAPI_VERSION"]
TP_UAPI_WSDL_VERSION = os.environ["TP_UAPI_WSDL_VERSION"]


class HotelCoordinateLocation(BaseModel):
    latitude: float
    longitude: float


class HotelDistance(BaseModel):
    Units: str = Field(default="KM", alias="units")
    Value: int = Field(alias="value")


class HotelSearchLocation(BaseModel):
    CoordinateLocation: HotelCoordinateLocation = Field(alias="coordinate_location")
    Distance: HotelDistance = Field(alias="distance")


class HotelSearchModifiers(BaseModel):
    NumberOfAdults: int = Field(default=1, alias="no_of_adults")
    NumberOfRooms: int = Field(default=1, alias="no_of_rooms")
    AvailableHotelsOnly: str = Field(default="true", alias="available_hotels_only")
    ReturnPropertyDescription: str = Field(default="true", alias="return_property_description")
    ReturnAmenities: str = Field(default="true", alias="return_amenities")
    AggregateResults: str = Field(default="true", alias="aggregate_results")
    PermittedProviders: dict = Field(default={"Provider": {"Code": "1G"}}, alias="permitted_providers")
    PreferredCurrency: str = Field(default="INR", alias="preferred_currency")
    PermittedChains: Optional[dict] = Field(default=None, alias="permitted_chains")


class HotelStayDetails(BaseModel):
    CheckinDate: str = Field(alias="check_in_date")
    CheckoutDate: str = Field(alias="check_out_date")


class GDSHotelRequest(BaseModel):
    TargetBranch: str
    TraceId: str = Field(default="12345")
    BillingPointOfSaleInfo: str = Field(default="UAPI")
    HotelSearchLocation: HotelSearchLocation
    HotelSearchModifiers: HotelSearchModifiers
    HotelStay: HotelStayDetails


class GDSHotels:
    def __init__(self, fre_config: HotelFREConfig) -> None:
        self.fre_config = fre_config
        wsdl = os.path.join(
            os.getcwd(),
            "app/services/hotelxmlconnector/hotel_vendor_request/lib/gds/" + TP_UAPI_VERSION + "/hotel_" + TP_UAPI_WSDL_VERSION + "_0",
            "Hotel.wsdl",
        )
        uname = AES_decryption_data(fre_config.uname)
        username = "Universal API/" + uname

        self.soap_manager = SOAPManager.get_instance(
            wsdl_url=wsdl,
            service_url=self.fre_config.end_point,
            username=username,
            password=self.fre_config.password,
            settings=Settings(strict=False, xml_huge_tree=True, raw_response=True),
        )
        self.params = {}
        self.is_first_page = False
        self.allowed_allied_marriott_chains = []

    def _request_handler(self, request):
        permitted_chains = (
            construct_permitted_chains(self.allowed_allied_marriott_chains) if self.allowed_allied_marriott_chains else None
        )

        gds_hotel_request = GDSHotelRequest(
            TargetBranch=self.fre_config.token_member_id,
            HotelSearchLocation=HotelSearchLocation(
                coordinate_location=HotelCoordinateLocation(
                    latitude=round(float(request.latitude), 4),
                    longitude=round(float(request.longitude), 4),
                ),
                distance=HotelDistance(units="KM", value=int(request.radius)),
            ),
            HotelSearchModifiers=HotelSearchModifiers(
                no_of_adults=request.no_of_adults,
                no_of_rooms=request.no_of_rooms,
                preferred_currency=request.currency,
                permitted_chains=permitted_chains,
            ),
            HotelStay=HotelStayDetails(
                check_in_date=request.check_in_date,
                check_out_date=request.checkout_date,
            ),
        ).dict()

        if permitted_chains is None:
            gds_hotel_request["HotelSearchModifiers"].pop("permitted_chains", None)

        return gds_hotel_request

    def connect(self):
        self.soap_manager.connect()

    def disconnect(self):
        self.soap_manager.disconnect()

    def get_response(
        self,
        hotel_search: HotelVendorRequestPayload,
        next_page_reference=None,
        leg_request_id=None,
        allowed_marriott_chains: Optional[list] = None,
    ):
        self.allowed_allied_marriott_chains = allowed_marriott_chains
        params = self._request_handler(hotel_search)
        self.params = params  # This will be used later for constructing the request payload in case of an error
        if next_page_reference:
            params["NextResultReference"] = {
                "ProviderCode": "1G",
                "_value_1": next_page_reference,
            }
        connector_start_time = datetime.now()

        # binding_service_url = "{http://www.travelport.com/service/hotel_v46_0}HotelSearchServiceBinding"
        binding_service_url = "{http://www.travelport.com/service/hotel_" + TP_UAPI_WSDL_VERSION + "_0}HotelSearchServiceBinding"
        response = self.soap_manager.send_request(binding_service_url, params)
        if not next_page_reference:
            hotel_search_request = self.soap_manager.create_message("service", **params)
            hotel_search_xml_request = str(ET.tostring(hotel_search_request, encoding="utf8", method="xml"), "utf-8")
            logger.info(f"Hotel search request: {hotel_search_xml_request}")
            self.is_first_page = True
        connector_end_time = datetime.now()
        execution_time = (connector_end_time - connector_start_time).total_seconds()
        logger.info(f"Time taken to finish GDS SOAP call is {execution_time}")

        # hotel_search_xml_request = str(ET.tostring(hotel_search_request, encoding='utf8',
        #                                            method='xml'), 'utf-8').replace("'", "\\'")

        push_newrelic_custom_event(
            newrelic_agent,
            NR_API_EXECUTION_EVENT,
            new_relic_custom_event={
                "api_type": HotelAPITypes.GDS_SEARCH.value,
                "execution_time": round(execution_time, 6),
                "api_status_code": response.status_code,
                "vendor": self.fre_config.name,
            },
        )

        return response, 500
