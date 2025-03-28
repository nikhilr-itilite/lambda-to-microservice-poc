import traceback
from enum import Enum
from datetime import datetime
from aws_lambda_powertools import Tracer
from helperlayer import leg_status_update, vendor_request_status_update

from opensearchlogger.logging import logger

tracer = Tracer()
MODE = "hotel"


class ConnectorStatus(Enum):
    PENDING = 0
    STARTED = 1
    SUCCESS = 2
    NO_RESULT = 3
    FAILED = 4
    NOT_IMPLEMENTED = 5


@tracer.capture_method(capture_response=False)
def update_connector_status(
    connector_status,
    batch_count,
    hotel_count,
    leg_request_id,
    vendor_request_id,
    start_time,
    end_time,
):
    """
    updates connector status in trip collection.
    :param connector_status:
    :param leg_request_id:
    :param vendor_request_id:
    :param batch_count:
    :param hotel_count:
    :return:
    """
    try:
        root_folder_date = datetime.now().strftime("%Y_%m_%d")
        vendor_request_status_update(
            vendor_request_id,
            "connector_status",
            connector_status,
            mode=MODE,
            layer_name="connector",
            total_batches=batch_count,
            total_hotels=hotel_count,
            start_time=start_time,
            end_time=end_time,
            vendor_call_date=root_folder_date,
        )
        leg_status_update(leg_request_id, "connector_status", mode=MODE)
    except Exception as general_exception:
        traceback.print_exc()
        logger.error(f"Error while updating connector status. error:{traceback.format_exc()}")
        raise Exception(general_exception) from general_exception


def construct_permitted_chains(chain_ids: list):
    chain_obj = {"HotelChain": []}

    for id in chain_ids:
        chain_obj["HotelChain"].append({"Code": id})

    return chain_obj


def batch_list(data, batch_size):
    """Splits the list into chunks of the given batch size."""
    for i in range(0, len(data), batch_size):
        yield data[i : i + batch_size]
