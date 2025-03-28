import concurrent.futures
import json
import os
import traceback
from datetime import datetime


import newrelic.agent
from aws_lambda_powertools import Tracer
from aws_lambda_powertools.utilities.typing import LambdaContext
from helperlayer import (
    HotelFREConfig,
    initialise_mongo_connector_helper_layer,
    is_warmer,
    InvalidHotelStayException,
    push_error_message,
    ItiliteBaseException,
    push_newrelic_custom_event,
)
from opensearchlogger.logging import logger, opensearch_logger
from s3connector import save_to_s3

from app.services.hotelxmlconnector.hotel_vendor_request import helper, lat_long_grid_derivation
from app.services.hotelxmlconnector.hotel_vendor_request.connector import HotelVendorRequestHandler
from app.services.hotelxmlconnector.hotel_vendor_request.helper import ConnectorStatus
from app.services.hotelxmlconnector.hotel_vendor_request.hotel import HotelVendorRequestPayload
from app.services.hotelxmlconnector.mongo_util import mongo_obj
from app.services.hotelxmlconnector import constants

HOTEL_REQUEST_DATE_FORMAT = os.environ["HOTEL_REQUEST_DATE_FORMAT"]
HOTEL_ERROR_S3_BUCKET = os.environ["HOTEL_ERROR_S3_BUCKET"]
DEGREE_OF_DEPTH = {"gds": 3, "desiya": 0, "ALLIED_US_PCC": 3}
MAX_THREAD_WORKER_COUNT = 20

newrelic_agent = newrelic.agent.register_application()
tracer = Tracer()


def _process_hotel_request(event, request_context, location):
    """
    Process a single hotel request.
    Args:
        event (dict): The original event payload.
        request_context (dict): Consolidated context for hotel request (hotel_request, trip_info, fre_config, etc.).
        location (tuple): A tuple containing latitude, longitude, and radius.
    """
    result = {}
    start_time = datetime.now()
    connector_status = ConnectorStatus.STARTED
    try:
        latitude, longitude, radius = location
        hotel_request = request_context["hotel_request"]
        trip_info = request_context["trip_info"]
        fre_config = request_context["fre_config"]
        vendor_currency_type = request_context["vendor_currency_type"]
        trip_id = request_context["trip_id"]
        request_payload = HotelVendorRequestPayload(
            check_in_date=hotel_request["checkin"],
            checkout_date=hotel_request["checkout"],
            latitude=latitude,
            longitude=longitude,
            city=hotel_request["location_details"]["city"],
            country=hotel_request["location_details"]["country"],
            no_of_rooms=trip_info["no_of_rooms_count"],  # TODO: Is this okay for multi-room call ?
            no_of_adults=int(trip_info["no_of_adults_count"]),
            no_of_children=int(trip_info["no_of_child_count"]),
            radius=radius,
            currency=vendor_currency_type,
        )
        vendor_request_handler = HotelVendorRequestHandler(
            fre_config=fre_config,
            hotel_request=request_payload,
            trip_id=trip_id,
            payload=event,
            sub_vendor_count=request_context["sub_vendor_count"],
        )
        result = vendor_request_handler.get_hotels_from_vendor()

        result.update(
            {
                "number_of_pages": vendor_request_handler.next_page_count,
            }
        )
        connector_status = ConnectorStatus.SUCCESS

        push_newrelic_custom_event(
            newrelic_agent,
            "SingleGDSHotelsCount",
            {
                "vendor_request_id": hotel_request["vendor_request_id"],
                "hotels_count": result.get("hotels", 0),
                "search location": hotel_request["location"],
                "check_in_date": hotel_request["checkin"],
                "check_out_date": hotel_request["checkout"],
                "city": hotel_request["location_details"]["city"],
            },
        )

        logger.info(f"Processed hotel request for location ({latitude}, {longitude}): {result}")
    # todo: improve code around exception and status handling
    except InvalidHotelStayException as ie:
        _handle_error(ie.message, request_context)  # Technically this is not a failure
        connector_status = ConnectorStatus.SUCCESS
    except ItiliteBaseException as pe:
        _handle_error(pe.message, request_context)
        connector_status = ConnectorStatus.NO_RESULT
    except NotImplementedError as nie:
        _handle_error(str(nie), request_context)
        connector_status = ConnectorStatus.NOT_IMPLEMENTED
    except Exception:
        connector_status = ConnectorStatus.FAILED
        logger.error(f"Error processing hotel request: {traceback.format_exc()}")
        raise
    finally:
        end_time = datetime.now()
        result.update(
            {
                "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S"),
                "time_taken": round((end_time - start_time).total_seconds(), 2),
                "connector_status": connector_status.value,
            }
        )
        return result


def _handle_error(error_message, request_context):
    """Handle errors by logging and pushing error data."""
    # connector_status = status
    error_data = request_context["log_data"]
    error_data["error"] = error_message
    push_error_message(json.dumps(error_data))
    save_to_s3(HOTEL_ERROR_S3_BUCKET, request_context["error_s3_path"], json.dumps(error_message))



@opensearch_logger(constants.SERVICE_NAME)
def handler(event, context: LambdaContext):
    connector_start_time = datetime.now()
    connector_status = ConnectorStatus.STARTED
    number_of_pages = 0
    total_hotels = 0
    batches = 0
    origin_country = None
    vendor = None

    try:
        trip_info = event["leg_req_info"]["trip_info"]
        trip_id = trip_info["trip_id"]
        hotel_request = event["leg_req_info"]["hotel_request"]
        leg_request_id = hotel_request["leg_request_id"]
        fre = event["fre_config"]
        vendor_request_id = fre["vendor_request_id"]
        log_data = {
            "trip_id": trip_id,
            "leg_request_id": leg_request_id,
            "vendor_request_id": vendor_request_id,
            "is_farequote_trigger": bool(fre.get("farequote_flag")),
            "service": constants.SERVICE_NAME,
        }
        os.environ["logging_unique_data"] = json.dumps(log_data)
        logger.info(f"{constants.SERVICE_NAME} Event : {event}")

        initialise_mongo_connector_helper_layer(mongo_obj)
        vendor_currency_type = fre["vendor_currency"]["type"]
        radius = hotel_request.get("radius", int(os.getenv("HOTEL_RADIUS", 0)))
        if not radius:
            raise ValueError("HOTEL_RADIUS is not configured")

        latitude = hotel_request["location_details"]["lat"]
        longitude = hotel_request["location_details"]["lng"]
        vendor = fre["name"]
        locations = lat_long_grid_derivation.get_equivalent_smaller_grids(latitude, longitude, radius, DEGREE_OF_DEPTH[vendor])
        logger.info(f"Original request data: {latitude} {longitude} {radius}" + f"\nNew smaller circles data: {locations}")
        origin_country = hotel_request.get("location_details", {}).get("country_short_name", "")

        request_context = {
            "hotel_request": hotel_request,
            "trip_info": trip_info,
            "fre_config": HotelFREConfig(**fre),
            "vendor_currency_type": vendor_currency_type,
            "trip_id": trip_id,
            "error_s3_path": f"{trip_id}/{leg_request_id}/{vendor_request_id}.json",
            "log_data": log_data,
            "sub_vendor_count": 1,
        }

        all_statuses = set()
        # Use ThreadPoolExecutor for concurrent processing
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREAD_WORKER_COUNT) as executor:
            futures = {}
            for loc in locations:
                future = executor.submit(_process_hotel_request, event, request_context, loc)
                futures[future] = loc
                request_context["sub_vendor_count"] += 1

            for future in concurrent.futures.as_completed(futures):
                loc = futures[future]
                try:
                    result = future.result()
                    number_of_pages += result.get("number_of_pages", 0)
                    batches += result.get("batches", 0)
                    total_hotels += result.get("hotels", 0)
                    logger.info(f"Success for location {loc}: {result}")
                    all_statuses.add(result["connector_status"])
                except Exception as exc:
                    logger.error(f"Error for location {loc}: {exc}")

        all_statuses = list(all_statuses)
        if ConnectorStatus.SUCCESS in all_statuses:
            connector_status = ConnectorStatus.SUCCESS
        elif ConnectorStatus.FAILED in all_statuses:
            connector_status = ConnectorStatus.FAILED
            raise Exception("Error while extracting vendor data, please refer to the error logs or google chat notification")
        elif ConnectorStatus.NO_RESULT in all_statuses:
            connector_status = ConnectorStatus.NO_RESULT
            raise ItiliteBaseException("No rates found for the given search criteria from the vendor")

    except Exception:
        logger.error(f"Error in {constants.SERVICE_NAME}: {traceback.format_exc()}")
        connector_status = ConnectorStatus.FAILED
    finally:
        connector_end_time = datetime.now()
        time_delta = (datetime.now() - connector_start_time).total_seconds()

        logger.info(
            f"Finished {constants.SERVICE_NAME}"
            f"\nHotels : {total_hotels} | Batches: {batches} | Pages: {number_of_pages} | vendor: {vendor} |\n"
            f"Duration: {round(time_delta, 3)} seconds | "
            f"Start Time: {connector_start_time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        helper.update_connector_status(
            connector_status.value,
            batches,
            total_hotels,
            leg_request_id,
            vendor_request_id,
            connector_start_time,
            connector_end_time,
        )

        push_newrelic_custom_event(
            newrelic_agent,
            "HotelXMLConnector",
            {
                "execution_time": round(time_delta, 6),
                "number_of_pages": number_of_pages,
                "number_of_hotels": total_hotels,
                "hotel_request_country": origin_country,
                "connector_status": connector_status.value,
            },
        )
