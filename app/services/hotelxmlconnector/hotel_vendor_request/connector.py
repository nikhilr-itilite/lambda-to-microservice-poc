import contextvars
import copy
import os
import traceback
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from enum import Enum
from io import BytesIO
from typing import Optional
import math

from aws_lambda_powertools import Tracer
from helperlayer import HotelFREConfig, ItiliteBaseException, datetime_format_converter, application_constants
from hotel_vendor_request.hotel import HotelVendorRequestPayload
from hotel_vendor_request.vendor.desiya import DesiyaHotels
from hotel_vendor_request.vendor.gds import GDSHotels
from kafkaconnector import KafkaConnector
from logger import OpensearchLogger
from lxml import etree
from mongo_util import mongo_obj
from opensearchlogger.logging import logger
from s3connector import S3Threading
from .helper import batch_list


# from .. import MODE


REQUEST_DATE_FORMAT = os.environ["HOTEL_REQUEST_DATE_FORMAT"]
STATIC_DATABASE = os.environ["STATIC_DATABASE"]
HOTEL_CHAIN_MEMBERSHIP_DEAL_COLLECTION = os.environ["HOTEL_CHAIN_MEMBERSHIP_DEAL_COLLECTION"]
TP_UAPI_VERSION = os.environ["TP_UAPI_VERSION"]
TP_UAPI_WSDL_VERSION = os.environ["TP_UAPI_WSDL_VERSION"]

MODE = "hotel"
HOTEL_CHAIN_MEMBERSHIP_DEAL_MAPPING = {}
tracer = Tracer()
processed_hotel_ids = {}

Allied_details = application_constants.AlliedDetailsConstants()
Allied_marriott_hotel_chains = list(Allied_details.ALLIED_MARRIOTT_HOTEL_CHAINS)
ALLIED_VENDOR = Allied_details.ALLIED_US_PCC


class ConnectorStatus(int, Enum):
    PENDING: int = 0
    STARTED: int = 1
    SUCCESS: int = 2
    NO_RESULT: int = 3
    FAILED: int = 4


VENDOR_REQUEST_MAPPING = {
    "gds": {
        "connector": GDSHotels,
        "result": "/x:Envelope/x:Body/y:HotelSearchAvailabilityRsp/y:HotelSearchResult",
        "error": "/x:Envelope/x:Body/x:Fault/faultstring",
        "namespaces": {
            "x": "http://schemas.xmlsoap.org/soap/envelope/",
            "y": "http://www.travelport.com/schema/hotel_" + TP_UAPI_WSDL_VERSION + "_0",
            "z": "http://www.travelport.com/schema/common_" + TP_UAPI_WSDL_VERSION + "_0",
        },
        "date_format": "%Y-%m-%d",
        "next_page_reference": "/x:Envelope/x:Body/y:HotelSearchAvailabilityRsp/z:NextResultReference",
        "allow_next_page_until": 15,
        "next_page_cut_off_time": 160,  # In sec
        "required_separate_hotel_details": True,
    },
    "desiya": {
        "connector": DesiyaHotels,
        "result": "/x:Envelope",
        "error": "/x:Envelope/x:Body/y:OTA_HotelAvailRS/y:Errors/y:Error",
        "namespaces": {
            "x": "http://schemas.xmlsoap.org/soap/envelope/",
            "y": "http://www.opentravel.org/OTA/2003/05",
        },
        "date_format": "%Y-%m-%d",
        "next_page_reference": None,
        "allow_next_page_until": 15,
        "next_page_cut_off_time": 160,  # In sec
        "required_separate_hotel_details": True,
    },
    "ALLIED_US_PCC": {
        "connector": GDSHotels,
        "result": "/x:Envelope/x:Body/y:HotelSearchAvailabilityRsp/y:HotelSearchResult",
        "error": "/x:Envelope/x:Body/x:Fault/faultstring",
        "namespaces": {
            "x": "http://schemas.xmlsoap.org/soap/envelope/",
            "y": "http://www.travelport.com/schema/hotel_" + TP_UAPI_WSDL_VERSION + "_0",
            "z": "http://www.travelport.com/schema/common_" + TP_UAPI_WSDL_VERSION + "_0",
        },
        "date_format": "%Y-%m-%d",
        "next_page_reference": "/x:Envelope/x:Body/y:HotelSearchAvailabilityRsp/z:NextResultReference",
        "allow_next_page_until": 15,
        "next_page_cut_off_time": 160,  # In sec
        "required_separate_hotel_details": True,
    },
}


def get_membership_deal_code_by_hotel_chain(hotel_chain):
    """
    This method fetches hickory deal codes or RoomRateDescription pattern of GDS hotel details calls
    which are used to get membership rates for the hotel chain.
    :param hotel_chain:     Hotel chain for which hickory membership deals or patterns to be fetched.
    :return:    Membership rate object
    """
    membership_mapping = {}
    try:
        global HOTEL_CHAIN_MEMBERSHIP_DEAL_MAPPING
        if not HOTEL_CHAIN_MEMBERSHIP_DEAL_MAPPING:
            HOTEL_CHAIN_MEMBERSHIP_DEAL_MAPPING = mongo_obj.find_one(STATIC_DATABASE, HOTEL_CHAIN_MEMBERSHIP_DEAL_COLLECTION, {})
        membership_mapping = HOTEL_CHAIN_MEMBERSHIP_DEAL_MAPPING.get(hotel_chain) or {}
        if membership_mapping:
            logger.info(f"Membership mapping for chain: {hotel_chain} is {membership_mapping}")
    except Exception:
        logger.error(f"Error while fetching membership deal codes for hotel_chain: {hotel_chain}. Error: {traceback.format_exc()}")
    finally:
        return membership_mapping


def _is_hotel_processed(hotel_id):
    if processed_hotel_ids.get(hotel_id):
        return True
    logger.info(f"Ignoring Hotel {hotel_id} as already processed")
    processed_hotel_ids[hotel_id] = True
    return False


class HotelVendorRequestHandler:
    def __init__(
        self,
        fre_config: HotelFREConfig,
        hotel_request: HotelVendorRequestPayload,
        trip_id: str,
        payload: dict,
        sub_vendor_count: int,
    ):
        self.fre_config = fre_config
        self.trip_id = trip_id
        self.payload = payload
        self.sub_vendor_count = sub_vendor_count

        # No of hotels
        self.hotel_count = 0
        self.batch_number = 0

        if self.fre_config.name not in VENDOR_REQUEST_MAPPING:
            raise NotImplementedError(f"hotel connector mapping is not present for {self.fre_config.name}")

        request_mapping = VENDOR_REQUEST_MAPPING[self.fre_config.name]

        self.request_mapping = request_mapping
        self.page_data = {"from": 451, "to": 500, "count": 500, "limit": 500}

        self.hotel_connector = self.request_mapping["connector"](self.fre_config)

        self.hotel_request = hotel_request

        self.hotel_request.check_in_date = datetime_format_converter(
            self.hotel_request.check_in_date,
            REQUEST_DATE_FORMAT,
            request_mapping["date_format"],
        )
        self.hotel_request.checkout_date = datetime_format_converter(
            self.hotel_request.checkout_date,
            REQUEST_DATE_FORMAT,
            request_mapping["date_format"],
        )

        self.next_page_count = 1
        self.connector_start_time = datetime.now()
        self.page_ref = False

        self.raw_xml_response = None
        self.root_xml = None
        parent_context = contextvars.copy_context()
        self.leg_request_id = self.payload["leg_req_info"]["hotel_request"]["leg_request_id"]
        self.thread_pool_executor = ThreadPoolExecutor(
            max_workers=15,
            initializer=OpensearchLogger.set_context,
            initargs=(parent_context,),
        )
        self.thread_pool_list = []

        self.s3 = S3Threading()

    @tracer.capture_method(capture_response=False)
    def get_hotels_from_vendor(self):
        try:
            self.hotel_connector.connect()
            if self.fre_config.name == "desiya":
                self.send_request_to_vendor(self.page_data)
            elif self.fre_config.name == ALLIED_VENDOR:
                batches = list(batch_list(Allied_marriott_hotel_chains, 3))
                max_workers = math.ceil(len(Allied_marriott_hotel_chains) / 3)
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    executor.map(lambda batch: self.send_request_to_vendor(None, batch), batches)
            else:
                self.send_request_to_vendor()
            # self.hotel_connector.disconnect()

            # print(self.raw_xml_response)

            status_code = 200
            status_message = "Vendor fetch is successful"
            return {"status_code": status_code, "message": status_message, "batches": self.batch_number, "hotels": self.hotel_count}

        except ItiliteBaseException as itilite_exception:
            logger.error(
                f"Vendor fetch exception: {itilite_exception} traceback - {traceback.format_exc()} "
                f"leg request id - {self.leg_request_id} vendor request id - {self.fre_config.vendor_request_id}"
            )
            for future in as_completed(self.thread_pool_list):
                try:
                    future.result()
                except ItiliteBaseException as itilite_exception:
                    logger.error(f"Thread hotel xml detail failed {itilite_exception.message}")
            if self.batch_number <= 1:
                raise ItiliteBaseException(itilite_exception.message) from itilite_exception
            else:
                status_code = 200
                status_message = "Vendor fetch is successful"
                return {"status_code": status_code, "message": status_message, "batches": self.batch_number}

    @tracer.capture_method(capture_response=False)
    def send_request_to_vendor(self, next_page_reference=None, allowed_marriott_chains: Optional[list] = None):
        if allowed_marriott_chains:
            logger.info(f"Processing batch for marriott: {allowed_marriott_chains}")
        raw_response, page_limit = self.hotel_connector.get_response(
            self.hotel_request, next_page_reference, self.leg_request_id, allowed_marriott_chains
        )
        if raw_response:
            if raw_response.status_code != 200:
                raise ItiliteBaseException(
                    f" Search call :Got error status code from the vendor while fetching next page"
                    f" reference:{raw_response.status_code} leg_request_id - {self.leg_request_id} "
                    f"vendor request id - {self.fre_config.vendor_request_id} "
                    f"next page refer - {next_page_reference}"
                )
            elif self.page_data["to"] != self.page_data["limit"]:
                self.page_data["from"] = page_limit + 1
                self.page_data["to"] = self.page_data["from"] + (self.page_data["count"] - 1)

            self.raw_xml_response = raw_response.content

            self.process_hotel_response()
        for future in as_completed(self.thread_pool_list):
            try:
                future.result()
            except ItiliteBaseException as itilite_exception:
                logger.error(
                    f"Hotel xml thread failed for one hotel - {itilite_exception.message} traceback - " f"{traceback.format_exc()}"
                )

    @tracer.capture_method(capture_response=False)
    def process_hotel_response(self):
        # Save the response in S3
        self.root_xml = etree.parse(BytesIO(self.raw_xml_response)).getroot()
        if self.fre_config.name == "desiya":
            try:
                s3_metadata = self.save_to_s3(self.raw_xml_response, 5000)
                logger.info(f"desiya_search_vendor_response_full_s3====> {s3_metadata}")
            except Exception:
                logger.error(traceback.format_exc())
            self.split_xml_string(self.raw_xml_response)
        else:
            s3_metadata = self.save_to_s3(self.raw_xml_response, self.next_page_count)

        self.check_for_error()

        # If no error, start sending the hotel to the hotel_details topic (parallel call)
        if self.request_mapping["required_separate_hotel_details"]:
            # Since we are using threading, we dont want to pass the root_xml reference to this function.
            try:
                if self.fre_config.name != "desiya":
                    self.thread_pool_list.append(
                        self.thread_pool_executor.submit(
                            self.send_to_hotel_details_topic,
                            copy.deepcopy(self.root_xml),
                            s3_metadata,
                        )
                    )
            except Exception:
                logger.error(traceback.format_exc())
        if self.fre_config.name == "desiya":
            self.page_ref = True
        else:
            # Check if next page is available. If yes, then send another parallel request
            next_page_reference = []
            if self.request_mapping["next_page_reference"]:
                next_page_reference = self.root_xml.xpath(
                    self.request_mapping["next_page_reference"],
                    namespaces=self.request_mapping["namespaces"],
                )

            current_time = datetime.now()
            total_execution_time = current_time - self.connector_start_time
            if (
                len(next_page_reference) > 0
                and len(next_page_reference[0].text) > 0
                and self.next_page_count < self.request_mapping.get("allow_next_page_until", 2)
                and total_execution_time.total_seconds() < self.request_mapping.get("next_page_cut_off_time", 180)
            ):
                self.next_page_count += 1
                self.send_request_to_vendor(next_page_reference[0].text)
            else:
                self.page_ref = True

    @tracer.capture_method(capture_response=False)
    def check_for_error(self):
        error = self.root_xml.xpath(self.request_mapping["error"], namespaces=self.request_mapping["namespaces"])

        if len(error) > 0:
            if self.fre_config.name == "desiya" and self.next_page_count > 1:
                logger.info(f"desiya_search_params----- error {error[0].text}")
                # self.page_data["to"] = self.page_data["limit"]
            else:
                if (
                    self.fre_config.name == "gds"
                    and hasattr(self.hotel_connector, "is_first_page")
                    and not self.hotel_connector.is_first_page
                ):
                    # This is duplicate call in case of a complete failure
                    hotel_search_request = self.hotel_connector.soap_manager.create_message(
                        "service", **self.hotel_connector.params
                    )
                    hotel_search_xml_request = str(ET.tostring(hotel_search_request, encoding="utf8", method="xml"), "utf-8")
                    logger.info(f"Hotel search request: {hotel_search_xml_request}")

                for error_message in error:
                    raise ItiliteBaseException(
                        f" Search call : Got error from the vendor: {error_message.text} +"
                        f" leg_request_id - {self.leg_request_id} "
                        f"vendor request - {self.fre_config.vendor_request_id}"
                        f" page count - {self.next_page_count} "
                    )

    def push_to_s3(self, each_data, xml_resp):
        try:
            s3_metadata = self.s3.save_to_s3(each_data["bucket"], each_data["filename"], each_data["data"])
            logger.info(f"desiya_search_vendor_response_split_s3====>  {s3_metadata}")
            self.send_to_hotel_details_topic(xml_resp, s3_metadata)

        except Exception:
            logger.error(f"Error in push_to_s3-----  {traceback.format_exc()}")

    def split_xml_string(self, xml_string, chunk_size=10):
        try:
            xpath = "/x:Envelope/x:Body/y:OTA_HotelAvailRS/y:RoomStays/y:RoomStay"
            parser = etree.XMLParser(remove_blank_text=True)
            root = etree.parse(BytesIO(xml_string), parser).getroot()

            elements = root.xpath(xpath, namespaces=self.request_mapping["namespaces"])
            chunk_count = 1
            count = 0
            chunk_elements = []
            s3_metadata_list = []
            xml_list = []
            for element in elements:
                chunk_elements.append(element)
                count += 1

                if count >= chunk_size:
                    resp, xml_root = self.get_s3_content(chunk_elements, chunk_count)
                    s3_metadata_list.append(resp)
                    xml_list.append(xml_root)
                    chunk_count += 1
                    chunk_elements = []
                    count = 0

            # Write the last chunk
            if chunk_elements:
                resp, xml_root = self.get_s3_content(chunk_elements, chunk_count)
                s3_metadata_list.append(resp)
                xml_list.append(xml_root)

            for idx, each_data in enumerate(s3_metadata_list):
                self.thread_pool_list.append(self.thread_pool_executor.submit(self.push_to_s3, each_data, xml_list[idx]))
            return []
        except Exception:
            logger.error(f"Error in desiya split xml string-----  {traceback.format_exc()}")
            return []

    def get_s3_content(self, elements, chunk_count):
        try:
            nsmap = self.request_mapping["namespaces"]
            root_tag = elements[0].getroottree().getroot().tag
            chunk_root = etree.Element(root_tag, nsmap=nsmap)

            for element in elements:
                chunk_root.append(element)

            # Serialize the chunk_root to a string
            xml_data = etree.tostring(chunk_root, pretty_print=True)

            s3_bucket = os.environ.get("HOTEL_S3_BUCKET_NAME")
            leg_request_id = self.leg_request_id
            root_folder_date = datetime.now().strftime("%Y_%m_%d")
            file_path = root_folder_date + "/" + leg_request_id + "/"
            s3_file_name = file_path + self.fre_config.vendor_request_id + "_" + str(chunk_count) + ".xml"
            resp = {"filename": s3_file_name, "bucket": s3_bucket, "data": xml_data}
            root = etree.parse(BytesIO(xml_data)).getroot()
            return resp, root
        except Exception:
            logger.info(f"error in getting s3 content---- {traceback.format_exc()}")

    @tracer.capture_method(capture_response=False)
    def save_to_s3(self, content, page_count):
        connector_start_time = datetime.now()

        s3_bucket = os.environ.get("HOTEL_S3_BUCKET_NAME")
        leg_request_id = self.leg_request_id
        root_folder_date = datetime.now().strftime("%Y_%m_%d")
        file_path = root_folder_date + "/" + leg_request_id + "/"
        if page_count == 5000:
            s3_file_name = file_path + self.fre_config.vendor_request_id + "_" + str(self.sub_vendor_count) + "_full_raw.xml"
        else:
            s3_file_name = (
                file_path + self.fre_config.vendor_request_id + "_" + str(self.sub_vendor_count) + "_" + str(page_count) + ".xml"
            )
        # s3_file_name = f"{MODE}/{str(self.trip_id)}/{str(self.fre_config.vendor_request_id)}_{page_count}.xml"
        metadata = self.s3.save_to_s3(s3_bucket, s3_file_name, content)

        connector_end_time = datetime.now()
        connector_delta = connector_end_time - connector_start_time
        logger.info(f"Time taken to finish S3 upload is {connector_delta.total_seconds()}")

        return metadata

    @tracer.capture_method(capture_response=False)
    def publish_to_topic(self, hotel_list, s3_metadata):
        # todo: rewrite this fun a lot of duplicate code
        leg_request_id = self.leg_request_id

        messages = {
            "batch_data": [],
            "kafka_topic": os.getenv("KAFKA_HOTEL_DETAILS_TOPIC"),
            "kafka_key": str(leg_request_id) + str(self.fre_config.vendor_id),
        }
        group_size = 5
        self.hotel_count += len(hotel_list)
        if self.fre_config.name == "desiya":
            group_size = 10
        if not self.page_ref:
            # todo: verify if total_batches calculation is correct
            for i in range(0, len(hotel_list), group_size):
                batch_data = {
                    "request_payload": self.payload,
                    "hotel_data": hotel_list[i : i + group_size],
                    "batch_num": self.batch_number,
                    "s3_metadata": s3_metadata,
                    "total_batches": 0,
                    "published_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                self.batch_number += 1
                messages["batch_data"] = batch_data
                KafkaConnector().produce(messages, is_parallel_push=True)
                check_connector_end_time = datetime.now()
                logger.info(
                    "Time taken to push particular page hotels to xml details - "
                    + str((check_connector_end_time - self.connector_start_time).total_seconds())
                )
                self.connector_start_time = datetime.now()
        else:
            for i in range(0, len(hotel_list), group_size):
                total_batches = 0
                if i + group_size >= len(hotel_list):
                    total_batches = self.batch_number
                batch_data = {
                    "request_payload": self.payload,
                    "hotel_data": hotel_list[i : i + group_size],
                    "batch_num": self.batch_number,
                    "s3_metadata": s3_metadata,
                    "total_batches": total_batches,
                    "published_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                self.batch_number += 1
                messages["batch_data"] = batch_data
                KafkaConnector().produce(messages, is_parallel_push=True)
                check_connector_end_time = datetime.now()
                logger.info(
                    "Time taken to push particular page hotels to xml details - "
                    + str((check_connector_end_time - self.connector_start_time).total_seconds())
                )
                self.connector_start_time = datetime.now()

    @tracer.capture_method(capture_response=False)
    def send_to_hotel_details_topic(self, root, s3_metadata):
        try:
            hotel_list = []
            hotel_result_xml = root.xpath(
                self.request_mapping["result"],
                namespaces=self.request_mapping["namespaces"],
            )

            total_hotel = len(hotel_result_xml)
            logger.info(f"Total no of hotels found: {str(total_hotel)}")

            for hotel in hotel_result_xml:
                data = {}
                if self.fre_config.name == "gds" or self.fre_config.name == ALLIED_VENDOR:
                    hotel_id = hotel.xpath(
                        "y:HotelProperty/@HotelCode",
                        namespaces=self.request_mapping["namespaces"],
                    )[0]
                    if _is_hotel_processed(hotel_id):
                        continue
                    data["hotel_id"] = hotel_id
                    hotel_chain = hotel.xpath(
                        "y:HotelProperty/@HotelChain",
                        namespaces=self.request_mapping["namespaces"],
                    )[0]
                    if self.fre_config.name == ALLIED_VENDOR and hotel_chain not in Allied_marriott_hotel_chains:
                        logger.info(f"skipped chain {hotel_chain}, not a marriott chain")
                        continue
                    data["hotel_chain"] = hotel_chain
                    data["membership_deal_mapping"] = get_membership_deal_code_by_hotel_chain(hotel_chain)
                    hotel_list.append(data)
                elif self.fre_config.name == "desiya":
                    hotel_ids = hotel.xpath(
                        "y:RoomStay/y:BasicPropertyInfo/@HotelCode",
                        namespaces=self.request_mapping["namespaces"],
                    )
                    hotel_ids = list(set(hotel_ids))
                    each_data = {}
                    for each_id in hotel_ids:
                        if _is_hotel_processed(each_id):
                            continue
                        each_data = {"hotel_id": each_id, "hotel_chain": None}
                        hotel_list.append(each_data)
            logger.info(f"Total no of hotel in hotel list {len(hotel_list)}")
            self.publish_to_topic(hotel_list, s3_metadata)
        except Exception as general_exception:
            logger.error(
                f"Search call - Error in sending the hotels to topic in thread: {general_exception}"
                f" leg_request_id - {self.leg_request_id} vendor request id - "
                f"{self.fre_config.vendor_request_id} hotel_list - {hotel_list} "
            )
            raise ItiliteBaseException(
                f"Search call - Error in sending the hotels to topic in thread: "
                f"{general_exception} leg_request_id - {self.leg_request_id} "
                f"vendor request id - {self.fre_config.vendor_request_id}"
                f" hotel_list - {hotel_list} "
            ) from general_exception
