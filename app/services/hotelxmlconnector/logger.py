from opensearchlogger.handlers import logging_unique_id


class OpensearchLogger:
    def __init__(self):
        logging_unique_id.set(
            {
                "leg_request_id": "",
                "trip_id": "",
                "vendor_request_id": "",
                "service": "",
            }
        )

    @staticmethod
    def set_logging_data(log_data):
        logging_unique_id.set(
            {
                "leg_request_id": log_data.get("leg_request_id") or "",
                "trip_id": log_data.get("trip_id") or "",
                "vendor_request_id": log_data.get("vendor_request_id") or "",
                "service": log_data.get("service") or "hotelxmlconnector",
            }
        )

    @staticmethod
    def set_context(context):
        for var, value in context.items():
            var.set(value)
