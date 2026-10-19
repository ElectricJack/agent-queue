from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.wait_ack_args import WaitAckArgs
from ...models.wait_ack_response import WaitAckResponse
from ...models.wait_ack_response_422 import WaitAckResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: WaitAckArgs,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/wait/ack",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> WaitAckResponse | WaitAckResponse422 | None:
    if response.status_code == 200:
        response_200 = WaitAckResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = WaitAckResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[WaitAckResponse | WaitAckResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: WaitAckArgs,
) -> Response[WaitAckResponse | WaitAckResponse422]:
    """Acknowledge a verified wait outcome for the held claim.

     Acknowledge a verified wait outcome for the held claim.

    Args:
        body (WaitAckArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[WaitAckResponse | WaitAckResponse422]
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
    body: WaitAckArgs,
) -> WaitAckResponse | WaitAckResponse422 | None:
    """Acknowledge a verified wait outcome for the held claim.

     Acknowledge a verified wait outcome for the held claim.

    Args:
        body (WaitAckArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        WaitAckResponse | WaitAckResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: WaitAckArgs,
) -> Response[WaitAckResponse | WaitAckResponse422]:
    """Acknowledge a verified wait outcome for the held claim.

     Acknowledge a verified wait outcome for the held claim.

    Args:
        body (WaitAckArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[WaitAckResponse | WaitAckResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: WaitAckArgs,
) -> WaitAckResponse | WaitAckResponse422 | None:
    """Acknowledge a verified wait outcome for the held claim.

     Acknowledge a verified wait outcome for the held claim.

    Args:
        body (WaitAckArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        WaitAckResponse | WaitAckResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
