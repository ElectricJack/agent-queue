from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.task_route_override_request import TaskRouteOverrideRequest
from ...models.task_route_override_response import TaskRouteOverrideResponse
from ...models.task_route_override_response_422 import TaskRouteOverrideResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: TaskRouteOverrideRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/route-override",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> TaskRouteOverrideResponse | TaskRouteOverrideResponse422 | None:
    if response.status_code == 200:
        response_200 = TaskRouteOverrideResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = TaskRouteOverrideResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[TaskRouteOverrideResponse | TaskRouteOverrideResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskRouteOverrideRequest,
) -> Response[TaskRouteOverrideResponse | TaskRouteOverrideResponse422]:
    """Emergency override: pin one queued task to a worker profile the router did not choose. Local
    operator and supervisor only; refused to workers, playbooks and every other token. Requires a reason
    (10-400 characters). The task is pinned to the profile's provider (failover holds it rather than
    moving it); the override is evented (task.route_overridden) and commented on the task. Refuses
    control, stage and role profiles, profiles that are not worker candidates, and a class the profile
    cannot run. `aq task route` clears it.

     Emergency override: pin one queued task to a worker profile the router did not choose. Local
    operator and supervisor only; refused to workers, playbooks and every other token. Requires a reason
    (10-400 characters). The task is pinned to the profile's provider (failover holds it rather than
    moving it); the override is evented (task.route_overridden) and commented on the task. Refuses
    control, stage and role profiles, profiles that are not worker candidates, and a class the profile
    cannot run. `aq task route` clears it.

    Args:
        body (TaskRouteOverrideRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskRouteOverrideResponse | TaskRouteOverrideResponse422]
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
    body: TaskRouteOverrideRequest,
) -> TaskRouteOverrideResponse | TaskRouteOverrideResponse422 | None:
    """Emergency override: pin one queued task to a worker profile the router did not choose. Local
    operator and supervisor only; refused to workers, playbooks and every other token. Requires a reason
    (10-400 characters). The task is pinned to the profile's provider (failover holds it rather than
    moving it); the override is evented (task.route_overridden) and commented on the task. Refuses
    control, stage and role profiles, profiles that are not worker candidates, and a class the profile
    cannot run. `aq task route` clears it.

     Emergency override: pin one queued task to a worker profile the router did not choose. Local
    operator and supervisor only; refused to workers, playbooks and every other token. Requires a reason
    (10-400 characters). The task is pinned to the profile's provider (failover holds it rather than
    moving it); the override is evented (task.route_overridden) and commented on the task. Refuses
    control, stage and role profiles, profiles that are not worker candidates, and a class the profile
    cannot run. `aq task route` clears it.

    Args:
        body (TaskRouteOverrideRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskRouteOverrideResponse | TaskRouteOverrideResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskRouteOverrideRequest,
) -> Response[TaskRouteOverrideResponse | TaskRouteOverrideResponse422]:
    """Emergency override: pin one queued task to a worker profile the router did not choose. Local
    operator and supervisor only; refused to workers, playbooks and every other token. Requires a reason
    (10-400 characters). The task is pinned to the profile's provider (failover holds it rather than
    moving it); the override is evented (task.route_overridden) and commented on the task. Refuses
    control, stage and role profiles, profiles that are not worker candidates, and a class the profile
    cannot run. `aq task route` clears it.

     Emergency override: pin one queued task to a worker profile the router did not choose. Local
    operator and supervisor only; refused to workers, playbooks and every other token. Requires a reason
    (10-400 characters). The task is pinned to the profile's provider (failover holds it rather than
    moving it); the override is evented (task.route_overridden) and commented on the task. Refuses
    control, stage and role profiles, profiles that are not worker candidates, and a class the profile
    cannot run. `aq task route` clears it.

    Args:
        body (TaskRouteOverrideRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskRouteOverrideResponse | TaskRouteOverrideResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: TaskRouteOverrideRequest,
) -> TaskRouteOverrideResponse | TaskRouteOverrideResponse422 | None:
    """Emergency override: pin one queued task to a worker profile the router did not choose. Local
    operator and supervisor only; refused to workers, playbooks and every other token. Requires a reason
    (10-400 characters). The task is pinned to the profile's provider (failover holds it rather than
    moving it); the override is evented (task.route_overridden) and commented on the task. Refuses
    control, stage and role profiles, profiles that are not worker candidates, and a class the profile
    cannot run. `aq task route` clears it.

     Emergency override: pin one queued task to a worker profile the router did not choose. Local
    operator and supervisor only; refused to workers, playbooks and every other token. Requires a reason
    (10-400 characters). The task is pinned to the profile's provider (failover holds it rather than
    moving it); the override is evented (task.route_overridden) and commented on the task. Refuses
    control, stage and role profiles, profiles that are not worker candidates, and a class the profile
    cannot run. `aq task route` clears it.

    Args:
        body (TaskRouteOverrideRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskRouteOverrideResponse | TaskRouteOverrideResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
