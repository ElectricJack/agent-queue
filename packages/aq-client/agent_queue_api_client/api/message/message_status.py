from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.message_status_request import MessageStatusRequest
from ...models.message_status_response import MessageStatusResponse
from ...models.message_status_response_422 import MessageStatusResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: MessageStatusRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/message/status",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> MessageStatusResponse | MessageStatusResponse422 | None:
    if response.status_code == 200:
        response_200 = MessageStatusResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = MessageStatusResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[MessageStatusResponse | MessageStatusResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: MessageStatusRequest,
) -> Response[MessageStatusResponse | MessageStatusResponse422]:
    """Report whether a queued message is queued, delivered, or acknowledged.

     Report whether a queued message is queued, delivered, or acknowledged.

    Args:
        body (MessageStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[MessageStatusResponse | MessageStatusResponse422]
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
    body: MessageStatusRequest,
) -> MessageStatusResponse | MessageStatusResponse422 | None:
    """Report whether a queued message is queued, delivered, or acknowledged.

     Report whether a queued message is queued, delivered, or acknowledged.

    Args:
        body (MessageStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        MessageStatusResponse | MessageStatusResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: MessageStatusRequest,
) -> Response[MessageStatusResponse | MessageStatusResponse422]:
    """Report whether a queued message is queued, delivered, or acknowledged.

     Report whether a queued message is queued, delivered, or acknowledged.

    Args:
        body (MessageStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[MessageStatusResponse | MessageStatusResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: MessageStatusRequest,
) -> MessageStatusResponse | MessageStatusResponse422 | None:
    """Report whether a queued message is queued, delivered, or acknowledged.

     Report whether a queued message is queued, delivered, or acknowledged.

    Args:
        body (MessageStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        MessageStatusResponse | MessageStatusResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
