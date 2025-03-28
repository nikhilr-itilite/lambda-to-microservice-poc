import os
import traceback

# import xml.etree.ElementTree as ET
from datetime import datetime

from helperlayer import HotelFREConfig, AES_decryption_data
from opensearchlogger.logging import logger
from s3connector import save_to_s3
import mysqlconnector as mysql
import requests

room_stay = (
    """<RoomStayCandidate>"""
    """<GuestCounts>"""
    """<GuestCount Age="" AgeQualifyingCode="10" />"""
    """</GuestCounts>"""
    """</RoomStayCandidate>"""
)

# room_stay equals the number of rooms
award_rating = [
    """<Award Rating="1" />""",
    """<Award Rating="2" />""",
    """<Award Rating="3" />""",
    """<Award Rating="4" />""",
    """<Award Rating="5" />""",
]


class DesiyaHotels:
    def __init__(self, fre_config: HotelFREConfig) -> None:
        self.fre_config = fre_config

    def connect(self):
        logger.info("im inside desiya connect")

    def disconnect(self):
        logger.info("im inside desiya disconnect")

    def get_response(self, hotel_search, search_param, leg_request_id, _):
        global room_stay
        global award_rating

        hotel_city = mysql.list_hotel_cities("api_slave", hotel_search.city, 1)

        if not hotel_city:
            if hotel_search.latitude and hotel_search.longitude:
                lat = str(hotel_search.latitude).split(".")
                lng = str(hotel_search.longitude).split(".")
                lat = lat[0] + "." + lat[1][0]
                lng = lng[0] + "." + lng[1][0]
                hotel_city = mysql.get_city_name("app", lat, lng)
                if not hotel_city:
                    return [], 500
            else:
                return [], 500

        cities = []
        # urls = []

        for row in hotel_city:
            if row.get("name") is not None:
                cities.append(row.get("name"))

        temp_cities = [city.upper() for city in cities]
        cities = list(set(temp_cities))

        country = hotel_search.country

        try:
            award_ratings = ""
            min_rate = 1
            end_point = self.fre_config.end_point
            password = AES_decryption_data(self.fre_config.password)
            property_id = AES_decryption_data(self.fre_config.property_id)
            uname = AES_decryption_data(self.fre_config.uname)
            desiya_url = end_point + "/TGServiceEndPoint"
            check_in = datetime.strptime(hotel_search.check_in_date, "%Y-%m-%d")
            check_out = datetime.strptime(hotel_search.checkout_date, "%Y-%m-%d")
            stay_in = datetime.strftime(check_in, "%Y-%m-%d")
            stay_out = datetime.strftime(check_out, "%Y-%m-%d")
            room_stays = room_stay * hotel_search.no_of_adults
            with open(os.path.join("./hotel_vendor_request/lib/desiya/desiya_search.xml")) as inp:
                desiya_search_params = inp.read()
            desiya_search_params = desiya_search_params.replace("\n", "")
            connector_start_time = datetime.now()
            if cities:
                logger.info(f"desiya search cities====>{cities}")
                city = cities[0]
                req_params = desiya_search_params.format(
                    city,
                    country,
                    stay_in,
                    stay_out,
                    min_rate,
                    room_stays,
                    award_ratings,
                    password,
                    str(property_id),
                    uname,
                )
                headers = {"Content-Type": "application/soap+xml; charset=utf-8"}
                logger.info(f"desiya_search_vendor_request====>   {req_params}")
                try:
                    root_folder_date = datetime.now().strftime("%Y_%m_%d")
                    file_path = root_folder_date + "/" + leg_request_id + "/"
                    s3_file_name = file_path + self.fre_config.vendor_request_id + "_vendor_request.xml"
                    s3_metadata = save_to_s3(
                        os.environ.get("HOTEL_S3_BUCKET_NAME"),
                        s3_file_name,
                        req_params,
                    )
                    logger.info(f"desiya_search_vendor_request_s3====>{s3_metadata}")
                except Exception:
                    logger.error(traceback.format_exc())
                response = requests.request("POST", desiya_url, headers=headers, data=req_params)
            else:
                logger.info(f"No city found for searched location {hotel_search}")
            connector_end_time = datetime.now()
            connector_delta = connector_end_time - connector_start_time
            logger.info(f"Time taken to finish Desiya API call is {connector_delta.total_seconds()}")
            return response, 500

        except Exception as ex:
            logger.info(f"DESIYA make_api_search_availability Request Exception in Desiya  {traceback.format_exc()}")
            logger.info(ex)
            return [], 500
