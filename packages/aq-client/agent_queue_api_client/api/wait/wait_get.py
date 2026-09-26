from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.wait_get_request import WaitGetRequest
from ...models.wait_get_response_422 import WaitGetResponse422
from ...models.wait_response import WaitResponse
from ...types import Response


def _get_kwargs(
    *,
    body: WaitGetRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/wait/get",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> WaitGetResponse422 | WaitResponse | None:
    if response.status_code == 200:
        response_200 = WaitResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = WaitGetResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[WaitGetResponse422 | WaitResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: WaitGetRequest,
) -> Response[WaitGetResponse422 | WaitResponse]:
    """Read a durable wait and its bounded result pointer.

     Read a durable wait and its bounded result pointer.

    Args:
        body (WaitGetRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[WaitGetResponse422 | WaitResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    body: WaitGetRequest,
) -> WaitGetResponse422 | WaitResponse | None:
    """Read a durable wait and its bounded result pointer.

     Read a durable wait and its bounded result pointer.

    Args:
        body (WaitGetRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        WaitGetResponse422 | WaitResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: WaitGetRequest,
) -> Response[WaitGetResponse422 | WaitResponse]:
    """Read a durable wait and its bounded result pointer.

     Read a durable wait and its bounded result pointer.

    Args:
        body (WaitGetRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[WaitGetResponse422 | WaitResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: WaitGetRequest,
) -> WaitGetResponse422 | WaitResponse | None:
    """Read a durable wait and its bounded result pointer.

     Read a durable wait and its bounded result pointer.

    Args:
        body (WaitGetRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        WaitGetResponse422 | WaitResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
