from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.pool_set_enabled_request import PoolSetEnabledRequest
from ...models.pool_set_enabled_response import PoolSetEnabledResponse
from ...models.pool_set_enabled_response_422 import PoolSetEnabledResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PoolSetEnabledRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/pool/set-enabled",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PoolSetEnabledResponse | PoolSetEnabledResponse422 | None:
    if response.status_code == 200:
        response_200 = PoolSetEnabledResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PoolSetEnabledResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PoolSetEnabledResponse | PoolSetEnabledResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PoolSetEnabledRequest,
) -> Response[PoolSetEnabledResponse | PoolSetEnabledResponse422]:
    """Turn a pool profile on or off on the system profile (it applies to every project). A disabled pool
    keeps its definition and its pool_status row but is handed no new work: idle workers drain, a worker
    mid-task finishes the task it holds, and task_claim answers drain_requested. Backs `aq pool set-
    enabled`.

     Turn a pool profile on or off on the system profile (it applies to every project). A disabled pool
    keeps its definition and its pool_status row but is handed no new work: idle workers drain, a worker
    mid-task finishes the task it holds, and task_claim answers drain_requested. Backs `aq pool set-
    enabled`.

    Args:
        body (PoolSetEnabledRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PoolSetEnabledResponse | PoolSetEnabledResponse422]
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
    body: PoolSetEnabledRequest,
) -> PoolSetEnabledResponse | PoolSetEnabledResponse422 | None:
    """Turn a pool profile on or off on the system profile (it applies to every project). A disabled pool
    keeps its definition and its pool_status row but is handed no new work: idle workers drain, a worker
    mid-task finishes the task it holds, and task_claim answers drain_requested. Backs `aq pool set-
    enabled`.

     Turn a pool profile on or off on the system profile (it applies to every project). A disabled pool
    keeps its definition and its pool_status row but is handed no new work: idle workers drain, a worker
    mid-task finishes the task it holds, and task_claim answers drain_requested. Backs `aq pool set-
    enabled`.

    Args:
        body (PoolSetEnabledRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PoolSetEnabledResponse | PoolSetEnabledResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PoolSetEnabledRequest,
) -> Response[PoolSetEnabledResponse | PoolSetEnabledResponse422]:
    """Turn a pool profile on or off on the system profile (it applies to every project). A disabled pool
    keeps its definition and its pool_status row but is handed no new work: idle workers drain, a worker
    mid-task finishes the task it holds, and task_claim answers drain_requested. Backs `aq pool set-
    enabled`.

     Turn a pool profile on or off on the system profile (it applies to every project). A disabled pool
    keeps its definition and its pool_status row but is handed no new work: idle workers drain, a worker
    mid-task finishes the task it holds, and task_claim answers drain_requested. Backs `aq pool set-
    enabled`.

    Args:
        body (PoolSetEnabledRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PoolSetEnabledResponse | PoolSetEnabledResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PoolSetEnabledRequest,
) -> PoolSetEnabledResponse | PoolSetEnabledResponse422 | None:
    """Turn a pool profile on or off on the system profile (it applies to every project). A disabled pool
    keeps its definition and its pool_status row but is handed no new work: idle workers drain, a worker
    mid-task finishes the task it holds, and task_claim answers drain_requested. Backs `aq pool set-
    enabled`.

     Turn a pool profile on or off on the system profile (it applies to every project). A disabled pool
    keeps its definition and its pool_status row but is handed no new work: idle workers drain, a worker
    mid-task finishes the task it holds, and task_claim answers drain_requested. Backs `aq pool set-
    enabled`.

    Args:
        body (PoolSetEnabledRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PoolSetEnabledResponse | PoolSetEnabledResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
