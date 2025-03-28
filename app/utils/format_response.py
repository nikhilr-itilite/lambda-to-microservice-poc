from typing import List

def format_requesthub_payload(payload: dict) -> List[dict]:
    """
    Format the payload coming from requesthub to connectors format
    :param payload: payload coming from requesthub
    :return: formatted payload for connectors
    """

    responses:List[dict] = []

    fre_configs:dict = payload.get("fre_config", {})
    if not fre_configs:
        return responses
    
    json_connectors: List[dict] = fre_configs.get("json_connector", [])
    xml_connectors: List[dict] = fre_configs.get("xml_connector", [])

    for connector in json_connectors:
        payload:dict = {
            "fre_config": connector,
            "leg_req_info": payload.get("leg_req_info", {}),
        }
        responses.append(payload)

    for connector in xml_connectors:
        payload:dict = {
            "fre_config": connector,
            "leg_req_info": payload.get("leg_req_info", {}),
        }
        responses.append(payload)   

    return responses