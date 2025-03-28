from fastapi import APIRouter, HTTPException, status
from app.services.hotelrequesthub import app
from app.services.hotelxmlconnector import app as hotelxmlconnector
from app.utils.format_response import format_requesthub_payload


router = APIRouter()


@router.get("/")
async def initiate_hotelrequesthub():
    test_event = {'trip_info': {'pre_fetch_request_id': '315c9f3e-43fc-4791-8ff8-9c5edf12f92d', 'trip_id': '0503-1691', 'version': 0, 'is_travel': 0, 'is_accommodation': 1, 'is_accomodation': 1, 'pre_fetch': 0, 'staff_id': 1362, 'round_trip': 0, 'one_way': 0, 'multistop': 0, 'trip_type': 2, 'travel_type': None, 'client_id': 503, 'parent_client_id': 503, 'is_personal': 0, 'staff_currency': {'type': 'USD', 'rate': 0.012}, 'client_currency': {'type': 'USD', 'rate': 0.012}, 'overall_travel_type': 'international', 'multioccupancy_rooms': 0, 'no_of_adults_count': 1, 'no_of_child_count': 0, 'no_of_infant_count': 0, 'no_of_rooms_count': 1, 'trip_requester': 1362, 'trip_status': 2, 'traveller_rewards': 1, 'travellers_staff_id': [1362], 'child_dob': [], 'actual_trip_id': None, 'trip_creation_utc': '2025-03-27 05:10:18', 'trip_unique_id': '67e4ddc419dfa38ee2aa42a6', 'multi_city': 0, 'trip_request_id': '0b667080-3a67-4544-8f0a-5765bd262208', 'hotel_corporate_deal': 0, 'flight_corporate_fare': 0, 'emp_level': 'L1', 'enable_membership_config': 1, 'enable_unused_ticket': 1, 'multicurrency': 1, 'instant_hotel_book': 1, 'enable_postpaid': 0, 'is_postpaid': 0, 'is_prepaid': 1, 'switch_postpaid_as_prepaid': 0, 'payment_method': 0, 'package_timer': '20', 'rt_split': 1, 'multicity': 0, 'cc_to_sc': 1.0, 'allow_window_break_hours': 0, 'updated_on': '2025-03-27 05:10:28.450914', 'enable_hotel_aaa_rate': 1}, 'hotel_request': {'leg_unique_id': '67e4ddc419dfa38ee2aa42a8', 'trip_unique_id': '67e4ddc419dfa38ee2aa42a6', 'leg_request_id': '67e4ddc419dfa38ee2aa42a7', 'trip_id': '0503-1691', 'leg_no': 1, 'status': 1, 'location': 'New York, NY, USA', 'checkin': '31 Mar, 2025', 'checkout': '01 Apr, 2025', 'is_location': 1, 'hotel_id': None, 'place_id': 'ChIJOwg_06VPwokRYv534QaPC8g', 'mode': 'hotel', 'location_details': {'country': 'United States', 'continent': '', 'region': 'New York', 'sub_region': '', 'political_locality': 'New York', 'city': 'New York', 'name': '', 'lat': 40.7127753, 'lng': -74.0059728, 'country_short_name': 'US'}, 'trip_creation_utc': '2025-03-27 05:10:18', 'created_on': '2025-03-27T05:10:27.395000', 'updated_on': '2025-03-27T05:10:28.553000'}}
    connector_requests = app.handler(test_event,None)
    if not connector_requests:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Hotel request hub failure",
        )
    
    connector_requests = format_requesthub_payload(connector_requests)

    # utilise multiprocessing here
    for connector_request in connector_requests:
        fre_config:dict = connector_request.get("fre_config")
        if fre_config.get("response_type") == "xml":
            print("*********************Initiating *****************************\n\n")
            hotelxmlconnector.handler(connector_request,None)
        else:
            continue
    
    return connector_requests