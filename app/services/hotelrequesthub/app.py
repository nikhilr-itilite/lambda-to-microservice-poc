import json
import os
import time
import traceback
from datetime import date
from datetime import datetime

import helperlayer as helpers
import mysqlconnector as mysql
import newrelic.agent
from bson.objectid import ObjectId
from helperlayer import push_error_message
from mysqlconnector import APP_DB, API_DB
from odmantic import SyncEngine
from opensearchlogger.logging import logger, opensearch_logger
from pydantic import ValidationError as PydanticValidationError

from app.services.hotelrequesthub.constants import mongo_obj
from app.services.hotelrequesthub.hotel_request import HotelRequest

newrelic.agent.initialize()

PAYMENT_BASED_CURRENCY = os.environ.get("PAYMENT_BASED_CURRENCY", 1)
HOTEL_REQUEST_DATE_FORMAT = os.environ["HOTEL_REQUEST_DATE_FORMAT"]
GROUP_BOOKING_PAX_COUNT = int(os.environ["GROUP_BOOKING_PAX_COUNT"])
GDS_VENDOR = "gds"


@opensearch_logger("hotelrequesthub")
def handler(event, context):
    log_data = {}
    lambda_start = datetime.now()
    hotel_id = None
    status_code = 200
    try:
        log_data = {
            "trip_id": event["trip_info"]["trip_id"],
            "leg_request_id": event["hotel_request"]["leg_request_id"],
            "service": "hotelrequesthub",
        }
        os.environ["logging_unique_data"] = json.dumps(log_data)
        # logger.info("Hotel Request hub was started at {}".format(str(lambda_start)))
        # logger.info(f"Input request : {event}")
        event = update_currency_value(event)
        start_event = time.time()
        travel_type = "domestic"
        trip_info = event["trip_info"]
        client_id = trip_info["client_id"]
        if trip_info["is_personal"] == 1 and trip_info["hotel_corporate_deal"] == 1:
            client_id = trip_info["parent_client_id"]

        location_details = event["hotel_request"]["location_details"]
        hotel_id = event["hotel_request"].get("hotel_id")

        origin_country = None
        if location_details["country_short_name"]:
            origin_country = location_details["country_short_name"]
        elif location_details["country"] and location_details["country"].lower() == "india":
            origin_country = "IN"
        if location_details["country"].lower() != "india":
            travel_type = "international"
        start_mysql = time.time()
        # logger.info("[Benchmarking] event to mysql process time ----------- {}".format(start_mysql - start_event))

        hotel_fre = mysql.get_hotel_fre(API_DB, client_id, travel_type, origin_country)
        if (
            len(hotel_fre["json_connector"]) == 0
            and len(hotel_fre["xml_connector"]) == 0
            and len(hotel_fre["itilite_connector"]) == 0
        ):
            hotel_fre = mysql.get_hotel_fre(API_DB, client_id, travel_type, "ALL")
        radius = mysql.get_hotel_geo_distance(API_DB, client_id)
        radius = float(radius[0]["geo_distance"]) if radius and len(radius) > 0 else None
        event["hotel_request"].update({"radius": radius})
        end_mysql = time.time()
        # logger.info("[Benchmarking] mysql fetch process time -----------{}".format(end_mysql - start_mysql))
        cph_fre = add_cph_fre()
        hotel_fre["itilite_connector"].append(cph_fre)
        date_delta = (
            datetime.strptime(event["hotel_request"]["checkout"], HOTEL_REQUEST_DATE_FORMAT).date()
            - datetime.strptime(event["hotel_request"]["checkin"], HOTEL_REQUEST_DATE_FORMAT).date()
        )
        number_of_nights = date_delta.days
        no_of_rooms_count = int(trip_info["no_of_rooms_count"])
        total_pax_count = int(trip_info["no_of_adults_count"]) + int(trip_info["no_of_child_count"])
        group_booking_case = True if total_pax_count >= GROUP_BOOKING_PAX_COUNT else False
        applicable_fre_found = 0
        max_stay_surpassed = 0
        max_room_surpassed = 0
        max_pax_surpassed = 0
        number_of_surpassed = 0

        msg = (
            f"Number of nights searched: {number_of_nights}, Number of rooms: {no_of_rooms_count}, "
            f"Number of pax: {total_pax_count}"
        )
        # logger.info(f"Hotel fre fetched {hotel_fre}")
        for each_connector in hotel_fre:
            for each_vendor in hotel_fre[each_connector]:
                if (
                    number_of_nights <= each_vendor.get("max_stay", 100)
                    and no_of_rooms_count <= each_vendor.get("max_room", 100)
                    and total_pax_count <= each_vendor.get("max_pax", 100)
                    and not group_booking_case
                ):
                    applicable_fre_found += 1
                else:
                    each_vendor.update({"threshold_surpassed": 1})
                    number_of_surpassed += 1
                    if group_booking_case:
                        msg += f"Removed {each_vendor['name']} from hotel request hub [Case of group booking]."
                    else:
                        msg += f"Hotel Stay/Room/PAX ROOM_LIMIT_EXCEEDED constraint violated for : {each_vendor['name']}"
                        if number_of_nights > each_vendor.get("max_stay", 100):
                            max_stay_surpassed += 1
                            msg += f" Max stay allowed: {each_vendor.get('max_stay', 100)}"
                        elif total_pax_count > each_vendor["max_pax"]:
                            max_pax_surpassed += 1
                            msg += f"Max Pax allowed: {each_vendor.get('max_pax', 100)} "
                        else:
                            max_room_surpassed += 1
                            msg += f" Max room allowed: {each_vendor.get('max_room', 100)}"

                    msg += " ..."

        fre_data = hotel_fre["json_connector"] + hotel_fre["xml_connector"] + hotel_fre["itilite_connector"]
        t_update_trip_doc_schema = time.time()
        trip_doc = update_trip_doc_schema(hotel_fre, event)
        # logger.info(
        #     "[Benchmarking] update_trip_doc_schema time : msg -----------{} {}".format(time.time() - t_update_trip_doc_schema, msg)
        # )
        event["hotel_request"]["cwm_id"] = fre_data[0]["cwm_id"]
        print("cache triggger --------------", event["hotel_request"])
        print("trigger_consolidation_lambda_cache triggger --------------", event)
        # cache_leg_id, cache_type = HotelRequest.find_cache(event['hotel_request'])
        # trip_doc_fetch = fetch_trip_info(event['trip_info']['trip_unique_id'],event['hotel_request']['leg_request_id'])
        # print("final hotel fre ---------",trip_doc_fetch)
        offline_request = None
        if applicable_fre_found == 0:
            if group_booking_case:
                offline_request = "PAX_LIMIT_EXCEEDED"
            elif max_pax_surpassed > 0:
                offline_request = "HOTEL_PAX_LIMIT_EXCEEDED"
            elif max_stay_surpassed > 0:
                offline_request = "STAY_LIMIT_EXCEEDED"
            elif max_room_surpassed > 0:
                offline_request = "ROOM_LIMIT_EXCEEDED"

        HotelRequest.update_cache_doc(event["hotel_request"], None, offline_request)
        if not offline_request:
            trigger_consolidation_lambda_cache({"body": event})
        else:
            error_data = log_data
            error_data["error"] = msg + " All vendor threshold surpassed."
            push_error_message(json.dumps(error_data))

        return {
            "status": 200,
            "fre_config": {
                "json_connector": obj_to_str(trip_doc["json"]),
                "xml_connector": obj_to_str(trip_doc["xml"]),
                "itilite_connector": obj_to_str(trip_doc["itilite"]),
            },
            "leg_req_info": event,
        }
    except Exception as ex:
        status_code = 500
        msg = f"Error formatting hotel FRE request data for trip_id : {str(ex)}, traceback - {traceback.format_exc()}"
        # logger.error(msg)
        push_error_message(msg)
        raise ex
    finally:
        helpers.push_newrelic_custom_event(
            newrelic.agent,
            "HotelRequesthub",
            {
                "hotel_id": hotel_id,  # Defines location search or named hotel search and the hotel id
                "execution_time": (datetime.now() - lambda_start).total_seconds(),
                "hotel_request_country": origin_country,
                "status_code": status_code,
            },
        )


def get_trip_engine():
    mongo_client = mongo_obj.get_client()
    trip_engine = SyncEngine(client=mongo_client, database=os.environ["TRIP_DB"])
    return trip_engine


def update_trip_doc_schema(hotel_fre, req_data):
    try:
        # start_mongo = time.time()
        trip_engine = get_trip_engine()
        client_id = req_data["trip_info"]["client_id"]
        fre_configs = hotel_fre["json_connector"] + hotel_fre["xml_connector"] + hotel_fre["itilite_connector"]
        all_fre_conf = []
        trip_data = trip_engine.find_one(
            helpers.Trip,
            helpers.Trip.trip_unique_id == ObjectId(req_data["trip_info"]["trip_unique_id"]),
        )
        trip_data.overall_request_status = 1
        trip_engine.save(trip_data)
        trip_info = json.loads(trip_data.json())
        for hotel_request_info in trip_info["hotel_request_info"]:
            data = trip_engine.find_one(
                helpers.HotelRequest,
                helpers.HotelRequest.leg_request_id == ObjectId(req_data["hotel_request"]["leg_request_id"]),
            )
            data.leg_request_status = 1
            # saved_hotel = json.loads(data.json())
            trip_engine.save(data)
        # trip_data = trip_engine.find_one(helpers.Trip, helpers.Trip.trip_id == req_data["trip_info"]["trip_id"])
        # start_mongo_fetch = time.time()
        # leg_no = req_data["hotel_request"]["leg_no"]
        req_hotel = req_data["hotel_request"]
        vendor_req = []
        # vendor_status = []
        hotel_vendor_req_ids = []
        # logger.info("hotel fre bef ---------------------- {}".format(hotel_fre))
        t_db_insert = datetime.now()
        with trip_engine.transaction() as transaction:
            vendor_request_id = ObjectId()
            req_hotel["vendor_request_id"] = vendor_request_id
            req_hotel["request_status"] = 1
            req_hotel["cwm_id"] = fre_configs[0]["cwm_id"]
            vendor_req = helpers.VendorRequest(**req_hotel)
            cc = req_data["trip_info"]["client_currency"]
            sc = req_data["trip_info"]["staff_currency"]
            # total_fre = len(fre_configs)
            env_conf = mysql.get_env_config_info(APP_DB, client_id)
            vendor_currency_objs = []
            vendor_currencies = set()
            for each_fre in fre_configs:  # Additional loop to reduce db calls
                _currency = get_vendor_currency(env_conf, client_id, cc, sc, each_fre["currency"])
                each_fre["derived_vendor_currency"] = _currency
                vendor_currencies.add(_currency["type"])
                vendor_currency_objs.append(_currency)

            vendor_currency_rate_map = fetch_currency_rate_map(list(vendor_currencies))
            for hotel_fre_data in fre_configs:
                try:
                    if hotel_fre_data.get("response_type", "") != "itilite" and not hotel_fre_data.get("end_point"):
                        # logger.info(f"None endpoint found in fre_config {hotel_fre_data}")
                        continue
                    currency_rate = {}
                    hotel_vendor_req_id = ObjectId()
                    hotel_fre_data["vendor_request_id"] = hotel_vendor_req_id
                    hotel_fre_data["trip_id"] = req_data["trip_info"]["trip_id"]
                    hotel_fre_data["trip_unique_id"] = req_data["trip_info"]["trip_unique_id"]
                    hotel_fre_data["leg_request_id"] = req_data["hotel_request"]["leg_request_id"]
                    hotel_fre_data["leg_unique_id"] = req_data["hotel_request"]["leg_unique_id"]
                    hotel_fre_data["request_status"] = 1
                    vendor_currency = hotel_fre_data["derived_vendor_currency"]
                    vc = vendor_currency_rate_map[vendor_currency["type"]]
                    # logger.info("fre currency ip -----------{} {} {}".format(vc, cc, sc))
                    currency_rate = helpers.get_currency_conv_rate(vc, cc, sc)
                    deal_codes = mysql.get_itilite_deal_codes_by_cvwdm_id(hotel_fre_data["cvwdm_id"], db_type=API_DB)
                    hotel_fre_data["deal_codes"] = deal_codes
                    if hotel_fre_data["name"] == GDS_VENDOR:
                        hotel_fre_data["company_deal_codes"] = mysql.get_deal_codes_for_client(
                            API_DB, client_id, hotel_fre_data["vendor_id"]
                        )
                    hotel_fre_data["vendor_currency"] = currency_rate["vc"]
                    hotel_fre_data["itilite_currency"] = currency_rate["ic"]
                    hotel_fre_data["staff_currency"] = currency_rate["sc"]
                    hotel_fre_data["client_currency"] = currency_rate["cc"]
                    if hotel_fre_data.get("threshold_surpassed", 0) == 1:
                        # SKIPPED BECAUSE OF THRESHOLD CROSSED OR GROUP BOOKING
                        hotel_fre_data.update({"connector_status": 9})
                    hotel_vendor_req = helpers.HotelVendorRequest(**hotel_fre_data)
                    hotel_vendor_req_ids.append(hotel_vendor_req_id)
                    transaction.save(hotel_vendor_req)
                    all_fre_conf.append(hotel_fre_data)
                except PydanticValidationError as e:
                    # logger.error(f"Error FRE configs : {traceback.format_exc()} : {e}")
                    continue
            vendor_req = helpers.VendorRequest(**req_hotel)
            vendor_req.hotel_vendor_req = hotel_vendor_req_ids
            transaction.save(vendor_req)
        # logger.info("[Benchmarking] Transaction took {}".format(datetime.now() - t_db_insert))
        # logger.info("in fre conf doccccccc----------- {}".format(all_fre_conf))
        json_conn = list(filter(lambda d: d["response_type"] == "json" and d.get("threshold_surpassed", 0) == 0, all_fre_conf))
        xml_conn = list(filter(lambda d: d["response_type"] == "xml" and d.get("threshold_surpassed", 0) == 0, all_fre_conf))
        itilite_conn = list(
            filter(lambda d: d["response_type"] == "itilite" and d.get("threshold_surpassed", 0) == 0, all_fre_conf)
        )

        result = {"xml": xml_conn, "json": json_conn, "itilite": itilite_conn}
        return result
    except Exception:
        print("error")
        # logger.error(f"Error while updating hotel vendor request document : {traceback.format_exc()}")


# def trigger_consolidation_lambda_cache(leg_info,cache_leg_id, cache_type):
def trigger_consolidation_lambda_cache(leg_info):
    try:
        # if cache_type is not None:
        #     if cache_type =="cold_cache":
        #         cold_cache_htl = {
        #             "hotel_request":leg_info["body"]["hotel_request"],
        #             "cache_leg_id":cache_leg_id,
        #             "cache_type":cache_type
        #         }
        #         leg_info["body"]["cache_type"] = cache_type
        #         cold_cache_req = {"type":"hotel","hotel":cold_cache_htl}
        #         response = trigger_cold_cache_lambda(cold_cache_req)
        #     else:
        #         leg_info["body"]["cache_leg_id"] = cache_leg_id
        #         leg_info["body"]["cache_type"] = cache_type
        #         leg_info["body"]["is_cache_request"] = True
        #         print("cache request------",leg_info)
        #         response = trigger_consolidation_lambda(leg_info)

        leg_info["body"]["is_cache_request"] = False
        # if leg_info.get("body",{}).get("cache_type",None) is not None:
        #     del leg_info["body"]["cache_type"]
        print("info request------", leg_info)
        t_start = datetime.now()
        response = trigger_consolidation_lambda(leg_info)
        # logger.info("[benchmarking] consolidation trigger took {}".format(str(datetime.now() - t_start)))
        return response
    except Exception:
        # logger.error(f"Error triggereing consolidation: {traceback.format_exc()}")
        return {}


def trigger_cold_cache_lambda(leg_info_data):
    try:
        # logger.info("trigger_cold_cache_lambda----------{}".format(leg_info_data))
        leg_info_data["hotel"]["hotel_request"]["vendor_request_id"] = str(
            leg_info_data["hotel"]["hotel_request"]["vendor_request_id"]
        )
        response = helpers.trigger_lambda(
            lambda_name=os.environ["COLD_CACHE_LAMBDA"],
            payload=json.dumps(leg_info_data),
        )
        # logger.info("lambda resp----------{}".format(response))
        return response
    except Exception:
        # logger.error(f"Error triggereing cold cache lambda: {traceback.format_exc()}")
        return {}


def trigger_consolidation_lambda(leg_info):
    try:
        # logger.info("trigger_consolidation_lambda----------{}".format(leg_info))
        leg_info["body"]["hotel_request"]["vendor_request_id"] = str(leg_info["body"]["hotel_request"]["vendor_request_id"])
        # logger.info("trigger_consolidation_lambda----------{}".format(leg_info))
        response = helpers.trigger_stepfunctions(
            state_machine_arn=os.environ["HOTEL_CONSOLIDATION_STATE_MACHINE_ARN"],
            state_machine_name=os.environ["HOTEL_CONSOLIDATION_STATE_MACHINE_NAME"],
            payload=json.dumps(leg_info),
        )
        return response
    except Exception:
        # logger.error(f"Error triggering hotel consolidation : {traceback.format_exc()}")
        return {}


def add_cph_fre():
    cph_fre = {
        "vendor_id": 0,
        "name": "cph",
        "response_type": "itilite",
        "cvwdm_id": 0,
        "wrapper": "agent",
        "trip_orign_country": "",
        "detail_id": 0,
        "markup_id": 0,
        "commision_id": 0,
        "currency": "INR",
        "property_id": "",
        "uname": "cph",
        "password": "",
        "end_point": "",
        "display_name": "Client Preferred Hotel",
        "token_member_id": "",
        "markup_type": None,
        "markup_value": None,
        "vendor_request_id": "",
        "cwm_id": 0,
        "dynamic_markup_policy": None,
    }
    return cph_fre


def vendor_currency_with_rate(vendor_currency):
    vc = {"type": "", "rate": ""}
    today_date = date.today()
    vc_db = mysql.get_currency_conversion(APP_DB, vendor_currency, today_date)
    if vc_db is not None:
        vc = {"type": vendor_currency, "rate": vc_db["currency_rate"]}
    return vc


def get_vendor_currency(env_conf, client_id, cc, sc, fre_currency):
    payment_method = env_conf[0]["payment_method"]
    payment_based_currency = env_conf[0]["payment_based_currency"]
    if int(payment_based_currency) == 1:
        if int(payment_method) == 0 or int(payment_method) == 2:
            return cc
        elif int(payment_method) == 1:
            return sc
    else:
        # fc = vendor_currency_with_rate(fre_currency)  # if we just use type and fetch rate later, is this necessary ?
        return {"type": fre_currency, "rate": None}


def fetch_currency_rate_map(currencies):
    """
    Fetch FX rate for the given vendor currencies
    :param currencies: List of currencies
    """
    try:
        func_start_time = datetime.now()
        TRIP_DB = os.getenv("TRIP_DB")
        CURRENCY_CONVERSION_COLLECTION = os.getenv("CURRENCY_CONVERSION_COLLECTION")
        # fetching the currecny conversion details
        base_currency = "INR"  # itilite default
        pipeline = [{"$match": {"base_currency": base_currency}}, {"$sort": {"date": -1}}, {"$limit": 1}]
        currency_conv_resp = mongo_obj.aggregate(TRIP_DB, CURRENCY_CONVERSION_COLLECTION, pipeline)
        currency_rate_map = {}
        if len(currency_conv_resp) > 0:
            if currency_conv_resp[0].get("rates") is not None:
                for currency in currencies:
                    currency_rate_map[currency] = {"rate": currency_conv_resp[0].get("rates").get(currency), "type": currency}
        diff = datetime.now() - func_start_time
        # logger.info(f"time to finish the fetch_currency_rate_map is {diff.total_seconds()}")
        return currency_rate_map
    except Exception as e:
        # logger.error(f"Couldn't fetch vendor currency rate. error: {traceback.format_exc()}")
        raise e


def update_currency_value(event):
    try:
        # logger.info("----------update_currency_value--------%s", event)
        trip_info = event.get("trip_info")
        if trip_info:
            staff_currency = event["trip_info"]["staff_currency"]["type"]
            client_currency = event["trip_info"]["client_currency"]["type"]

            event["trip_info"]["staff_currency"]["type"] = "".join(staff_currency.split()) if (staff_currency) else staff_currency
            event["trip_info"]["client_currency"]["type"] = (
                "".join(client_currency.split()) if (client_currency) else client_currency
            )
    except Exception as ex:
        print(ex)
        # logger.error("------ERROR while update_currency_value %s,%s-----%s", trip_info, event, ex, exc_info=True)
    finally:
        # logger.info("----------update_currency_value result--------%s", event)
        return event


def obj_to_str(data):
    if isinstance(data, dict):
        return {obj_to_str(key): obj_to_str(value) for key, value in data.items()}
    elif isinstance(data, list):
        return [obj_to_str(element) for element in data]
    elif isinstance(data, ObjectId):
        return str(data)
    else:
        return data


# def fetch_trip_info(trip_unique_id, leg_request_id):
#     try:
#         result = {"xml": [], "json": [], "itilite": []}
#         trip_info = json.loads(
#             TRIP_ENGINE.find_one(helpers.Trip, helpers.Trip.trip_unique_id == ObjectId(trip_unique_id)).json())
#         # logger.info("trip_info {}".format(trip_info))
#         hotels = {}
#         vendor_req = []
#         vendor_data = json.loads(
#             TRIP_ENGINE.find_one(
#                 helpers.VendorRequest,
#                 helpers.VendorRequest.leg_request_id == ObjectId(leg_request_id),
#             ).json()
#         )
#         # logger.info("vendor_data -------- {}".format(vendor_data))
#
#         for vendor_req_fre in vendor_data["hotel_vendor_req"]:
#             vendor_req_data = TRIP_ENGINE.find_one(
#                 helpers.HotelVendorRequest,
#                 helpers.HotelVendorRequest.vendor_request_id == ObjectId(vendor_req_fre),
#             )
#             vendor_req.append(json.loads(vendor_req_data.json()))
#         # logger.info("vendors------- {}".format(vendor_req))
#
#         json_conn = list(filter(lambda d: d["response_type"] == "json", vendor_req))
#         xml_conn = list(filter(lambda d: d["response_type"] == "xml", vendor_req))
#         itilite_conn = list(filter(lambda d: d["response_type"] == "itilite", vendor_req))
#
#         result = {"xml": xml_conn, "json": json_conn, "itilite": itilite_conn}
#
#         # logger.info("final result vendors ------------- {}".format(result))
#         return result
#     except Exception as ex:
#         # logger.error(f"Error while creating request: {traceback.format_exc()}")
#         raise ex
