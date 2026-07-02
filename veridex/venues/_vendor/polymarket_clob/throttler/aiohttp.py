import aiohttp
import asyncio
import inspect
import logging

from veridex.venues._vendor.polymarket_clob.throttler.exceptions import HTTPException

async def request(**request_args):
    """
    Perform an HTTP request.

    Args:
        request_args (dict): A dictionary of request arguments.

    Returns:
        dict: The response JSON.

    Raises:
        HTTPException: If the request fails with a non-200 status code.
    """
    async with aiohttp.ClientSession() as session:
        async with session.request(**request_args) as resp:
            status = resp.status
            if status == 200:
                return await resp.json()
            else:
                err = await resp.text()
                headers = resp.headers
                raise HTTPException(
                    status_code=status,
                    message=err,
                    headers=headers,
                    cargs=request_args
                )

class NetworkException(Exception):
    """
    An exception class for network errors, capturing the status code, error message, function name, and class name.

    Args:
        status_code (str, optional): The HTTP status code of the error. Defaults to an empty string.
        message (str, optional): The error message. Defaults to 'Network error occurred'.

    Attributes:
        status_code (str): The HTTP status code of the error.
        message (str): The error message.
        function_name (str): The name of the function where the error occurred.
        class_name (str): The name of the class where the error occurred.
        descriptive_message (str): The complete error message including function and class name.
    """

    def __init__(self, status_code="", message='Network error occurred'):
        frame = inspect.currentframe().f_back
        function_name = frame.f_code.co_name
        class_name = frame.f_locals.get('self', frame.f_locals.get('cls', '')).__class__.__name__
        descriptive_message = f'NetworkException :: {function_name} of {class_name} class: {message} :: {status_code}'
        self.function_name = function_name
        self.class_name = class_name
        self.descriptive_message = descriptive_message
        super().__init__(message)


async def asession_requests_get(urls, asemaphore=None, costs=None, refunds_in=None):
    """
    Perform asynchronous HTTP GET requests.

    Args:
        urls (list): A list of URLs to request.
        asemaphore (AsyncRateSemaphore, optional): An asynchronous semaphore to limit concurrent requests. Defaults to None.
        costs (list, optional): A list of costs for each request in terms of semaphore credits. Defaults to None.
        refunds_in (list, optional): A list of refund times for each request. Defaults to None.

    Returns:
        list: A list of responses, with exceptions if any occurred.

    Raises:
        NetworkException: If a request fails with a non-200 status code.
    """
    async with aiohttp.ClientSession() as session:
        async def fetch(url):
            logging.debug(url, extra={"network": "HTTP", "data": {}})
            async with session.get(url) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise NetworkException(status_code=response.status)

        if not asemaphore:
            return await asyncio.gather(
                *[fetch(url) for url in urls], return_exceptions=True
            )
        else:
            return await asyncio.gather(
                *[asemaphore.transact(
                    coroutine=fetch(url),
                    credits=cost,
                    refund_time=refund
                ) for url, cost, refund in zip(urls, costs, refunds_in)], return_exceptions=True
            )


async def asession_requests_post(urls, payloads, asemaphore=None, costs=None, refunds_in=None):
    """
    Perform asynchronous HTTP POST requests.

    Args:
        urls (list): A list of URLs to request.
        payloads (list): A list of payloads to send in the POST requests.
        asemaphore (AsyncRateSemaphore, optional): An asynchronous semaphore to limit concurrent requests. Defaults to None.
        costs (list, optional): A list of costs for each request in terms of semaphore credits. Defaults to None.
        refunds_in (list, optional): A list of refund times for each request. Defaults to None.

    Returns:
        list: A list of responses, with exceptions if any occurred.

    Raises:
        NetworkException: If a request fails with a non-200 status code.
    """
    async with aiohttp.ClientSession() as session:
        async def fetch(url, payload):
            logging.debug(url, extra={"network": "HTTP", "data": payload})
            async with session.post(url, json=payload, headers={"Content-Type": "application/json"}) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    raise NetworkException(status_code=response.status)

        if not asemaphore:
            return await asyncio.gather(
                *[fetch(url, payload) for url, payload in zip(urls, payloads)], return_exceptions=True
            )
        else:
            return await asyncio.gather(
                *[asemaphore.transact(
                    coroutine=fetch(url, payload),
                    credits=cost,
                    refund_time=refund
                ) for url, payload, cost, refund in zip(urls, payloads, costs, refunds_in)], return_exceptions=True
            )
