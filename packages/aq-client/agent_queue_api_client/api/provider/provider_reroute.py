from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.provider_reroute_request import ProviderRerouteRequest
from ...models.provider_reroute_response import ProviderRerouteResponse
from ...models.provider_reroute_response_422 import ProviderRerouteResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ProviderRerouteRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/provider/reroute",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ProviderRerouteResponse | ProviderRerouteResponse422 | None:
    if response.status_code == 200:
        response_200 = ProviderRerouteResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ProviderRerouteResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ProviderRerouteResponse | ProviderRerouteResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderRerouteRequest,
) -> Response[ProviderRerouteResponse | ProviderRerouteResponse422]:
    """Re-route queued work off an unavailable provider (provider failover).  Moves eligible queued and
    provider-paused tasks to the same intelligence class on an available provider, a pool-width at a
    time (provider_failover.reroute limits), and records each move on the task (undo with
    provider_reroute_undo).  Pinned tasks, single-provider classes (astra-*) and classes set to 'hold'
    stay where they are.  --dry-run plans only.  Naming tasks with --task-id plus --to-profile or
    --force is an explicit operator move; --force may move a pinned task, target a degraded provider or
    change the class.  Operators and supervisors only.

     Re-route queued work off an unavailable provider (provider failover).  Moves eligible queued and
    provider-paused tasks to the same intelligence class on an available provider, a pool-width at a
    time (provider_failover.reroute limits), and records each move on the task (undo with
    provider_reroute_undo).  Pinned tasks, single-provider classes (astra-*) and classes set to 'hold'
    stay where they are.  --dry-run plans only.  Naming tasks with --task-id plus --to-profile or
    --force is an explicit operator move; --force may move a pinned task, target a degraded provider or
    change the class.  Operators and supervisors only.

    Args:
        body (ProviderRerouteRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProviderRerouteResponse | ProviderRerouteResponse422]
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
    body: ProviderRerouteRequest,
) -> ProviderRerouteResponse | ProviderRerouteResponse422 | None:
    """Re-route queued work off an unavailable provider (provider failover).  Moves eligible queued and
    provider-paused tasks to the same intelligence class on an available provider, a pool-width at a
    time (provider_failover.reroute limits), and records each move on the task (undo with
    provider_reroute_undo).  Pinned tasks, single-provider classes (astra-*) and classes set to 'hold'
    stay where they are.  --dry-run plans only.  Naming tasks with --task-id plus --to-profile or
    --force is an explicit operator move; --force may move a pinned task, target a degraded provider or
    change the class.  Operators and supervisors only.

     Re-route queued work off an unavailable provider (provider failover).  Moves eligible queued and
    provider-paused tasks to the same intelligence class on an available provider, a pool-width at a
    time (provider_failover.reroute limits), and records each move on the task (undo with
    provider_reroute_undo).  Pinned tasks, single-provider classes (astra-*) and classes set to 'hold'
    stay where they are.  --dry-run plans only.  Naming tasks with --task-id plus --to-profile or
    --force is an explicit operator move; --force may move a pinned task, target a degraded provider or
    change the class.  Operators and supervisors only.

    Args:
        body (ProviderRerouteRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProviderRerouteResponse | ProviderRerouteResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderRerouteRequest,
) -> Response[ProviderRerouteResponse | ProviderRerouteResponse422]:
    """Re-route queued work off an unavailable provider (provider failover).  Moves eligible queued and
    provider-paused tasks to the same intelligence class on an available provider, a pool-width at a
    time (provider_failover.reroute limits), and records each move on the task (undo with
    provider_reroute_undo).  Pinned tasks, single-provider classes (astra-*) and classes set to 'hold'
    stay where they are.  --dry-run plans only.  Naming tasks with --task-id plus --to-profile or
    --force is an explicit operator move; --force may move a pinned task, target a degraded provider or
    change the class.  Operators and supervisors only.

     Re-route queued work off an unavailable provider (provider failover).  Moves eligible queued and
    provider-paused tasks to the same intelligence class on an available provider, a pool-width at a
    time (provider_failover.reroute limits), and records each move on the task (undo with
    provider_reroute_undo).  Pinned tasks, single-provider classes (astra-*) and classes set to 'hold'
    stay where they are.  --dry-run plans only.  Naming tasks with --task-id plus --to-profile or
    --force is an explicit operator move; --force may move a pinned task, target a degraded provider or
    change the class.  Operators and supervisors only.

    Args:
        body (ProviderRerouteRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProviderRerouteResponse | ProviderRerouteResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderRerouteRequest,
) -> ProviderRerouteResponse | ProviderRerouteResponse422 | None:
    """Re-route queued work off an unavailable provider (provider failover).  Moves eligible queued and
    provider-paused tasks to the same intelligence class on an available provider, a pool-width at a
    time (provider_failover.reroute limits), and records each move on the task (undo with
    provider_reroute_undo).  Pinned tasks, single-provider classes (astra-*) and classes set to 'hold'
    stay where they are.  --dry-run plans only.  Naming tasks with --task-id plus --to-profile or
    --force is an explicit operator move; --force may move a pinned task, target a degraded provider or
    change the class.  Operators and supervisors only.

     Re-route queued work off an unavailable provider (provider failover).  Moves eligible queued and
    provider-paused tasks to the same intelligence class on an available provider, a pool-width at a
    time (provider_failover.reroute limits), and records each move on the task (undo with
    provider_reroute_undo).  Pinned tasks, single-provider classes (astra-*) and classes set to 'hold'
    stay where they are.  --dry-run plans only.  Naming tasks with --task-id plus --to-profile or
    --force is an explicit operator move; --force may move a pinned task, target a degraded provider or
    change the class.  Operators and supervisors only.

    Args:
        body (ProviderRerouteRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProviderRerouteResponse | ProviderRerouteResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
