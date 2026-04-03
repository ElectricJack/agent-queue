from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.schedule_hook_request import ScheduleHookRequest
from ...models.schedule_hook_response import ScheduleHookResponse
from ...models.schedule_hook_response_422 import ScheduleHookResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ScheduleHookRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/hooks/schedule",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ScheduleHookResponse | ScheduleHookResponse422 | None:
    if response.status_code == 200:
        response_200 = ScheduleHookResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ScheduleHookResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ScheduleHookResponse | ScheduleHookResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ScheduleHookRequest,
) -> Response[ScheduleHookResponse | ScheduleHookResponse422]:
    """Schedule a one-shot hook to fire at a specific time or after a delay. The hook runs once, executes
    its prompt with full tool access, then auto-deletes. Use for deferred work: reminders, delayed
    checks, timed actions. Provide either 'fire_at' (epoch/ISO datetime) or 'delay' (e.g. '30m', '2h',
    '1d').

     Schedule a one-shot hook to fire at a specific time or after a delay. The hook runs once, executes
    its prompt with full tool access, then auto-deletes. Use for deferred work: reminders, delayed
    checks, timed actions. Provide either 'fire_at' (epoch/ISO datetime) or 'delay' (e.g. '30m', '2h',
    '1d').

    Args:
        body (ScheduleHookRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ScheduleHookResponse | ScheduleHookResponse422]
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
    body: ScheduleHookRequest,
) -> ScheduleHookResponse | ScheduleHookResponse422 | None:
    """Schedule a one-shot hook to fire at a specific time or after a delay. The hook runs once, executes
    its prompt with full tool access, then auto-deletes. Use for deferred work: reminders, delayed
    checks, timed actions. Provide either 'fire_at' (epoch/ISO datetime) or 'delay' (e.g. '30m', '2h',
    '1d').

     Schedule a one-shot hook to fire at a specific time or after a delay. The hook runs once, executes
    its prompt with full tool access, then auto-deletes. Use for deferred work: reminders, delayed
    checks, timed actions. Provide either 'fire_at' (epoch/ISO datetime) or 'delay' (e.g. '30m', '2h',
    '1d').

    Args:
        body (ScheduleHookRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ScheduleHookResponse | ScheduleHookResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ScheduleHookRequest,
) -> Response[ScheduleHookResponse | ScheduleHookResponse422]:
    """Schedule a one-shot hook to fire at a specific time or after a delay. The hook runs once, executes
    its prompt with full tool access, then auto-deletes. Use for deferred work: reminders, delayed
    checks, timed actions. Provide either 'fire_at' (epoch/ISO datetime) or 'delay' (e.g. '30m', '2h',
    '1d').

     Schedule a one-shot hook to fire at a specific time or after a delay. The hook runs once, executes
    its prompt with full tool access, then auto-deletes. Use for deferred work: reminders, delayed
    checks, timed actions. Provide either 'fire_at' (epoch/ISO datetime) or 'delay' (e.g. '30m', '2h',
    '1d').

    Args:
        body (ScheduleHookRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ScheduleHookResponse | ScheduleHookResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ScheduleHookRequest,
) -> ScheduleHookResponse | ScheduleHookResponse422 | None:
    """Schedule a one-shot hook to fire at a specific time or after a delay. The hook runs once, executes
    its prompt with full tool access, then auto-deletes. Use for deferred work: reminders, delayed
    checks, timed actions. Provide either 'fire_at' (epoch/ISO datetime) or 'delay' (e.g. '30m', '2h',
    '1d').

     Schedule a one-shot hook to fire at a specific time or after a delay. The hook runs once, executes
    its prompt with full tool access, then auto-deletes. Use for deferred work: reminders, delayed
    checks, timed actions. Provide either 'fire_at' (epoch/ISO datetime) or 'delay' (e.g. '30m', '2h',
    '1d').

    Args:
        body (ScheduleHookRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ScheduleHookResponse | ScheduleHookResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
