from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.schedule_get_response_422 import ScheduleGetResponse422
from ...models.schedule_pointer_args import SchedulePointerArgs
from ...models.schedule_response import ScheduleResponse
from ...types import Response


def _get_kwargs(
    *,
    body: SchedulePointerArgs,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/schedule/get",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ScheduleGetResponse422 | ScheduleResponse | None:
    if response.status_code == 200:
        response_200 = ScheduleResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ScheduleGetResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ScheduleGetResponse422 | ScheduleResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SchedulePointerArgs,
) -> Response[ScheduleGetResponse422 | ScheduleResponse]:
    """Read a project schedule and its firing/cancellation receipt.

     Read a project schedule and its firing/cancellation receipt.

    Args:
        body (SchedulePointerArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ScheduleGetResponse422 | ScheduleResponse]
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
    body: SchedulePointerArgs,
) -> ScheduleGetResponse422 | ScheduleResponse | None:
    """Read a project schedule and its firing/cancellation receipt.

     Read a project schedule and its firing/cancellation receipt.

    Args:
        body (SchedulePointerArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ScheduleGetResponse422 | ScheduleResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SchedulePointerArgs,
) -> Response[ScheduleGetResponse422 | ScheduleResponse]:
    """Read a project schedule and its firing/cancellation receipt.

     Read a project schedule and its firing/cancellation receipt.

    Args:
        body (SchedulePointerArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ScheduleGetResponse422 | ScheduleResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: SchedulePointerArgs,
) -> ScheduleGetResponse422 | ScheduleResponse | None:
    """Read a project schedule and its firing/cancellation receipt.

     Read a project schedule and its firing/cancellation receipt.

    Args:
        body (SchedulePointerArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ScheduleGetResponse422 | ScheduleResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
