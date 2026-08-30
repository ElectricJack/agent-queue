from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.get_stuck_tasks_request import GetStuckTasksRequest
from ...models.get_stuck_tasks_response import GetStuckTasksResponse
from ...models.get_stuck_tasks_response_422 import GetStuckTasksResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: GetStuckTasksRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/get-stuck-tasks",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> GetStuckTasksResponse | GetStuckTasksResponse422 | None:
    if response.status_code == 200:
        response_200 = GetStuckTasksResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = GetStuckTasksResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[GetStuckTasksResponse | GetStuckTasksResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetStuckTasksRequest,
) -> Response[GetStuckTasksResponse | GetStuckTasksResponse422]:
    r"""Return tasks stuck in ASSIGNED or IN_PROGRESS beyond their per-status threshold.  Detection and time
    arithmetic run in the database — the caller passes thresholds and a reference ``now`` timestamp and
    receives a structured list back.  Defaults match the system-health-check playbook's stuck
    definition: ASSIGNED > 30 minutes, IN_PROGRESS > 2 hours.  Each entry carries ``id``,
    ``project_id``, ``status``, ``assigned_agent``, ``updated_at``, and ``seconds_in_state`` so
    remediation (``restart_task`` vs ``set_task_status(..., status=\"READY\")``) can branch on the agent
    state.

     Return tasks stuck in ASSIGNED or IN_PROGRESS beyond their per-status threshold.  Detection and time
    arithmetic run in the database — the caller passes thresholds and a reference ``now`` timestamp and
    receives a structured list back.  Defaults match the system-health-check playbook's stuck
    definition: ASSIGNED > 30 minutes, IN_PROGRESS > 2 hours.  Each entry carries ``id``,
    ``project_id``, ``status``, ``assigned_agent``, ``updated_at``, and ``seconds_in_state`` so
    remediation (``restart_task`` vs ``set_task_status(..., status=\"READY\")``) can branch on the agent
    state.

    Args:
        body (GetStuckTasksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetStuckTasksResponse | GetStuckTasksResponse422]
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
    body: GetStuckTasksRequest,
) -> GetStuckTasksResponse | GetStuckTasksResponse422 | None:
    r"""Return tasks stuck in ASSIGNED or IN_PROGRESS beyond their per-status threshold.  Detection and time
    arithmetic run in the database — the caller passes thresholds and a reference ``now`` timestamp and
    receives a structured list back.  Defaults match the system-health-check playbook's stuck
    definition: ASSIGNED > 30 minutes, IN_PROGRESS > 2 hours.  Each entry carries ``id``,
    ``project_id``, ``status``, ``assigned_agent``, ``updated_at``, and ``seconds_in_state`` so
    remediation (``restart_task`` vs ``set_task_status(..., status=\"READY\")``) can branch on the agent
    state.

     Return tasks stuck in ASSIGNED or IN_PROGRESS beyond their per-status threshold.  Detection and time
    arithmetic run in the database — the caller passes thresholds and a reference ``now`` timestamp and
    receives a structured list back.  Defaults match the system-health-check playbook's stuck
    definition: ASSIGNED > 30 minutes, IN_PROGRESS > 2 hours.  Each entry carries ``id``,
    ``project_id``, ``status``, ``assigned_agent``, ``updated_at``, and ``seconds_in_state`` so
    remediation (``restart_task`` vs ``set_task_status(..., status=\"READY\")``) can branch on the agent
    state.

    Args:
        body (GetStuckTasksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetStuckTasksResponse | GetStuckTasksResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetStuckTasksRequest,
) -> Response[GetStuckTasksResponse | GetStuckTasksResponse422]:
    r"""Return tasks stuck in ASSIGNED or IN_PROGRESS beyond their per-status threshold.  Detection and time
    arithmetic run in the database — the caller passes thresholds and a reference ``now`` timestamp and
    receives a structured list back.  Defaults match the system-health-check playbook's stuck
    definition: ASSIGNED > 30 minutes, IN_PROGRESS > 2 hours.  Each entry carries ``id``,
    ``project_id``, ``status``, ``assigned_agent``, ``updated_at``, and ``seconds_in_state`` so
    remediation (``restart_task`` vs ``set_task_status(..., status=\"READY\")``) can branch on the agent
    state.

     Return tasks stuck in ASSIGNED or IN_PROGRESS beyond their per-status threshold.  Detection and time
    arithmetic run in the database — the caller passes thresholds and a reference ``now`` timestamp and
    receives a structured list back.  Defaults match the system-health-check playbook's stuck
    definition: ASSIGNED > 30 minutes, IN_PROGRESS > 2 hours.  Each entry carries ``id``,
    ``project_id``, ``status``, ``assigned_agent``, ``updated_at``, and ``seconds_in_state`` so
    remediation (``restart_task`` vs ``set_task_status(..., status=\"READY\")``) can branch on the agent
    state.

    Args:
        body (GetStuckTasksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetStuckTasksResponse | GetStuckTasksResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: GetStuckTasksRequest,
) -> GetStuckTasksResponse | GetStuckTasksResponse422 | None:
    r"""Return tasks stuck in ASSIGNED or IN_PROGRESS beyond their per-status threshold.  Detection and time
    arithmetic run in the database — the caller passes thresholds and a reference ``now`` timestamp and
    receives a structured list back.  Defaults match the system-health-check playbook's stuck
    definition: ASSIGNED > 30 minutes, IN_PROGRESS > 2 hours.  Each entry carries ``id``,
    ``project_id``, ``status``, ``assigned_agent``, ``updated_at``, and ``seconds_in_state`` so
    remediation (``restart_task`` vs ``set_task_status(..., status=\"READY\")``) can branch on the agent
    state.

     Return tasks stuck in ASSIGNED or IN_PROGRESS beyond their per-status threshold.  Detection and time
    arithmetic run in the database — the caller passes thresholds and a reference ``now`` timestamp and
    receives a structured list back.  Defaults match the system-health-check playbook's stuck
    definition: ASSIGNED > 30 minutes, IN_PROGRESS > 2 hours.  Each entry carries ``id``,
    ``project_id``, ``status``, ``assigned_agent``, ``updated_at``, and ``seconds_in_state`` so
    remediation (``restart_task`` vs ``set_task_status(..., status=\"READY\")``) can branch on the agent
    state.

    Args:
        body (GetStuckTasksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetStuckTasksResponse | GetStuckTasksResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
