import os
import random
import traceback
import urllib.parse
from datetime import datetime

import requests
from helperlayer import get_cache_key_hotel, LegEvent

from constants import mongo_obj
from opensearchlogger.logging import logger


class HotelRequest:
    def get_geo_location_details(self, location):
        """
        Reverse geo-coding to get the location details- latitude, longitude,country,region,sub-region

        """
        try:
            loc = {
                "country": "",
                "continent": "",
                "region": "",
                "sub_region": "",
                "political_locality": "",
                "city": "",
                "name": "",
            }
            google_map_url = os.environ["GOOGLE_GEOCODE_URL"]
            google_map_api_keys = ["AIzaSyA404fbBx-Y59-XYPEj6nu4ofKpAfbTaKM"]
            flocation = urllib.parse.quote(location.replace(" ", "+"))
            flocation = urllib.parse.quote(location.replace(" ", "+"))
            map_index = int(random.uniform(0, len(google_map_api_keys)))
            url = google_map_url + flocation + "&key=" + google_map_api_keys[map_index]
            print(url)
            resp = requests.get(url)
            print(resp)
            if resp:
                geo_resp = resp.json()
                geo_result = geo_resp["results"][0]

                loc["lat"] = geo_result["geometry"]["location"]["lat"]
                loc["lng"] = geo_result["geometry"]["location"]["lng"]
                for value in geo_result["address_components"]:
                    if "country" in value["types"]:
                        loc["country"] = value["long_name"]
                        loc["country_short_name"] = value["short_name"]
                    if "administrative_area_level_1" in value["types"]:
                        loc["region"] = value["long_name"]
                    if "administrative_area_level_2" in value["types"]:
                        loc["sub_region"] = value["long_name"]
                    if "locality" in value["types"]:
                        loc["political_locality"] = value["long_name"]
                        loc["city"] = value["long_name"]
                    # country_replace wrt env_Config todo
                    # continent - country_continent.json load redis todo
            print(loc)
            return loc
        except Exception:
            logger.error(f"Error fetching location details - reverse geo-coding : {traceback.format_exc()}")

    def find_cache(self, leg_info):
        # check_in_date = datetime.strptime(leg_info["checkin"], "%d %b, %Y").date().strftime("%Y_%m_%d")
        # check_out_date = datetime.strptime(leg_info["checkout"], "%d %b, %Y").date().strftime("%Y_%m_%d")
        # cache_key = leg_info["location_details"]["city"].lower()+"_"+check_in_date+"_"+check_out_date
        # warm_cache_key = leg_info["location_details"]["city"].lower()
        # cache_key = leg_info["location"]+"_"+leg_info["checkin"]+"_"+leg_info["checkout"]
        # warm_cache_key = leg_info["location"]
        city = leg_info["location_details"]["city"].lower()
        country = leg_info["location_details"]["country"].lower()
        cache_key, cold_cache_key = get_cache_key_hotel(city, country, leg_info["checkin"], leg_info["checkout"])
        mongo_client = mongo_obj.get_client()
        conso_db = mongo_client[os.environ["GLOBAL_CONSO_CACHE_DB_NAME"]]
        hotel_conso_collection = conso_db[os.environ["GLOBAL_CACHE_HOTEL_COLLECTION_NAME"]]
        cache_data = hotel_conso_collection.find_one({"_id": cache_key})
        cache_type = None
        cache_leg_id = None
        if cache_data is not None:
            cache_leg_id = cache_data.get("leg_request_id")
            updated_at = cache_data.get("update_at")
            time_diff_for_cache = ((datetime.now() - updated_at).total_seconds()) / 60
            if cache_type is None:
                if time_diff_for_cache <= float(os.environ["HOT_CACHE_TIME_MINUTES_HOTEL"]):
                    cache_type = "hot_cache"
                elif time_diff_for_cache <= float(os.environ["WARM_CACHE_TIME_MINUTES_HOTEL"]):
                    cache_type = "warm_cache"
                else:
                    cache_type = "cold_cache"
        if cache_data is None:
            cache_type = "cold_cache"
            cache_data = hotel_conso_collection.find_one({"_id": cold_cache_key})
            if cache_data is not None:
                cache_leg_id = cache_data.get("leg_request_id")

        HotelRequest.update_cache_doc(leg_info, cache_type)
        return cache_leg_id, cache_type

    @staticmethod
    def update_cache_doc(leg_info, cache_type, offline_request=None):
        print("leg info ---------", leg_info)
        message = None if not offline_request else offline_request
        status = "started" if not offline_request else "failed"
        doc = {
            "type": "recommendation",
            "status": status,
            "message": message,
            "insert_tn": datetime.now(),
            "upsert_tn": datetime.now(),
        }
        leg_event = LegEvent()
        leg_event.insert_leg_event("hotel", str(leg_info["leg_request_id"]), doc)
        return True
