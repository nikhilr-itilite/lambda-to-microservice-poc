import base64
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import helperlayer as helpers
import newrelic.agent
from helperlayer import push_newrelic_custom_event
from requests import Session
from zeep import Client, Transport

import constants

_hotel_vendor_reqs_soap_manager_instances = {}
POOL_SIZE = 40  # Maximum SOAPManager instances
MAX_RETRIES = 2  # Retry connection setup


class SOAPManager:
    _lock = threading.Lock()

    def __init__(self, wsdl_url, service_url, username=None, password=None, settings=None):
        encrypted_password = helpers.AES_decryption_data(password)
        self.wsdl_url = wsdl_url
        self.username = username
        self.password = encrypted_password.lstrip("b").strip(" ' ")
        self.settings = settings
        self.service_url = service_url
        self.client = None
        self.binding_service = None

    def connect(self):
        """Connect to the SOAP service."""
        if self.client:
            return  # Avoid reconnecting if already connected
        session = Session()
        auth_bytes = bytes(
            f"{self.username}:{self.password}",
            encoding="utf-8",
        )
        auth_base64 = base64.b64encode(auth_bytes).decode("utf-8")
        session.headers = {"Authorization": "Basic " + auth_base64}

        self.client = Client(
            self.wsdl_url,
            transport=Transport(session=session),
            settings=self.settings,
        )

    def disconnect(self):
        """Disconnect from the SOAP service."""
        self.client = None
        self.binding_service = None

    def send_request(self, service_name, request):
        """
        Send a SOAP request using the connected service.

        Args:
            service_name (str): The name of the service.
            request (dict): The SOAP request payload as a dictionary.

        Returns:
            The response from the SOAP service.
        """
        if self.client is None or self.binding_service is None:
            # Ensure the connection and binding service are active.
            self.connect()
            self.binding_service = self.create_service(service_name)

        return self.binding_service.service(**request)

    def create_service(self, service_name):
        """
        Create a service binding for the specified service name.

        Args:
            service_name (str): The name of the service to create.

        Returns:
            A service binding object for the specified service name.
        """
        if self.client is None:
            raise ValueError("Not connected to the SOAP service. Call connect() first.")
        return self.client.create_service(service_name, self.service_url)

    def create_message(self, message_name, **kwargs):
        """
        Create a message object for the specified message name.

        Args:
            message_name (str): The name of the message to create.
            **kwargs: Keyword arguments representing the input parameters for the message.

        Returns:
            A message object for the specified message name.
        """
        if self.client is None or self.binding_service is None:
            raise ValueError("Not connected to the SOAP service. Call connect() first.")
        return self.client.create_message(self.binding_service, message_name, **kwargs)

    @classmethod
    def get_instance(cls, wsdl_url, service_url, username, password, settings):
        """
        Retrieve a SOAPManager instance from the pool.

        Args:
            wsdl_url (str): WSDL URL of the SOAP service.
            service_url (str): SOAP service endpoint URL.
            username (str): Username for authentication.
            password (str): Password for authentication.
            settings: Additional SOAP client settings.

        Returns:
            SOAPManager: An instance from the pool.
        """
        global _hotel_vendor_reqs_soap_manager_instances
        start_time = datetime.now()
        is_cache = cls.initialize_pool(wsdl_url, service_url, username, password, settings)
        key = (wsdl_url, service_url, username)
        with cls._lock:
            pool, current_index = _hotel_vendor_reqs_soap_manager_instances[key]
            if not pool or not all(manager.client and manager.client.wsdl for manager in pool):
                # cache is corrupted, force initialise.
                is_cache = cls.initialize_pool(wsdl_url, service_url, username, password, settings, True)
                pool, current_index = _hotel_vendor_reqs_soap_manager_instances[key]
            _hotel_vendor_reqs_soap_manager_instances[key] = (pool, (current_index + 1) % len(pool))

        client = pool[current_index]
        time_delta = (datetime.now() - start_time).total_seconds()
        push_newrelic_custom_event(
            newrelic.agent,
            constants.SOAP_CLIENT_INIT_EVENT_NAME,
            {"module": constants.SERVICE_NAME, "latency": round(time_delta, 6), "is_cache": is_cache},
        )
        return client

    @classmethod
    def initialize_pool(cls, wsdl_url, service_url, username, password, settings, force_init=False):
        """
        Initialize a pool of SOAPManager instances for the specified service.

        Args:
            wsdl_url (str): WSDL URL of the SOAP service.
            service_url (str): SOAP service endpoint URL.
            username (str): Username for authentication.
            password (str): Password for authentication.
            settings: Additional SOAP client settings.
            force_init (bool): Force reinitialization of the pool.

        Returns:
            bool: True if the pool was retrieved from cache, False if it was reinitialized.
        """
        global _hotel_vendor_reqs_soap_manager_instances

        def create_and_connect_manager():
            """
            Create and connect a SOAPManager instance with retries.
            """
            manager = cls(wsdl_url, service_url, username, password, settings)
            for attempt in range(MAX_RETRIES):
                try:
                    manager.connect()
                    return manager
                except Exception as e:
                    if attempt == MAX_RETRIES - 1:
                        raise RuntimeError(f"Failed to initialize SOAPManager: {e}")

        key = (wsdl_url, service_url, username)
        is_cache = True

        with cls._lock:
            if force_init or key not in _hotel_vendor_reqs_soap_manager_instances:
                is_cache = False
                pool = []

                with ThreadPoolExecutor(max_workers=POOL_SIZE) as executor:
                    futures = [executor.submit(create_and_connect_manager) for _ in range(POOL_SIZE)]
                    [pool.append(future.result()) for future in as_completed(futures)]

                _hotel_vendor_reqs_soap_manager_instances[key] = pool, 0

        return is_cache
