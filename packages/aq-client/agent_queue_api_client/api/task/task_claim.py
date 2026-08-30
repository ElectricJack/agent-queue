from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.task_claim_request import TaskClaimRequest
from ...models.task_claim_response import TaskClaimResponse
from ...models.task_claim_response_422 import TaskClaimResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: TaskClaimRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/claim",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> TaskClaimResponse | TaskClaimResponse422 | None:
    if response.status_code == 200:
        response_200 = TaskClaimResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = TaskClaimResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[TaskClaimResponse | TaskClaimResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskClaimRequest,
) -> Response[TaskClaimResponse | TaskClaimResponse422]:
    """Claim a ready task for the calling pool/task session (pull-based work selection, swarm-work-model
    §10). Pass `next: true` for the next available task matching the session's profile, or `task_id` for
    a specific one. `wait` long-polls (clamped to `swarm.claim_wait_max`) instead of returning
    `no_ready_work` immediately. Writes `.aq/claim.json` and returns the task's own row plus
    `claim_epoch`, which subsequent `task_close` / `task_heartbeat` / `task_set` / `task_handoff` calls
    must echo back. The returned task is the row only — for dependencies, dependents, subtasks,
    children, context and labels, call `task_show` (`aq task show <id>`), which is the full view.

     Claim a ready task for the calling pool/task session (pull-based work selection, swarm-work-model
    §10). Pass `next: true` for the next available task matching the session's profile, or `task_id` for
    a specific one. `wait` long-polls (clamped to `swarm.claim_wait_max`) instead of returning
    `no_ready_work` immediately. Writes `.aq/claim.json` and returns the task's own row plus
    `claim_epoch`, which subsequent `task_close` / `task_heartbeat` / `task_set` / `task_handoff` calls
    must echo back. The returned task is the row only — for dependencies, dependents, subtasks,
    children, context and labels, call `task_show` (`aq task show <id>`), which is the full view.

    Args:
        body (TaskClaimRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskClaimResponse | TaskClaimResponse422]
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
    body: TaskClaimRequest,
) -> TaskClaimResponse | TaskClaimResponse422 | None:
    """Claim a ready task for the calling pool/task session (pull-based work selection, swarm-work-model
    §10). Pass `next: true` for the next available task matching the session's profile, or `task_id` for
    a specific one. `wait` long-polls (clamped to `swarm.claim_wait_max`) instead of returning
    `no_ready_work` immediately. Writes `.aq/claim.json` and returns the task's own row plus
    `claim_epoch`, which subsequent `task_close` / `task_heartbeat` / `task_set` / `task_handoff` calls
    must echo back. The returned task is the row only — for dependencies, dependents, subtasks,
    children, context and labels, call `task_show` (`aq task show <id>`), which is the full view.

     Claim a ready task for the calling pool/task session (pull-based work selection, swarm-work-model
    §10). Pass `next: true` for the next available task matching the session's profile, or `task_id` for
    a specific one. `wait` long-polls (clamped to `swarm.claim_wait_max`) instead of returning
    `no_ready_work` immediately. Writes `.aq/claim.json` and returns the task's own row plus
    `claim_epoch`, which subsequent `task_close` / `task_heartbeat` / `task_set` / `task_handoff` calls
    must echo back. The returned task is the row only — for dependencies, dependents, subtasks,
    children, context and labels, call `task_show` (`aq task show <id>`), which is the full view.

    Args:
        body (TaskClaimRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskClaimResponse | TaskClaimResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskClaimRequest,
) -> Response[TaskClaimResponse | TaskClaimResponse422]:
    """Claim a ready task for the calling pool/task session (pull-based work selection, swarm-work-model
    §10). Pass `next: true` for the next available task matching the session's profile, or `task_id` for
    a specific one. `wait` long-polls (clamped to `swarm.claim_wait_max`) instead of returning
    `no_ready_work` immediately. Writes `.aq/claim.json` and returns the task's own row plus
    `claim_epoch`, which subsequent `task_close` / `task_heartbeat` / `task_set` / `task_handoff` calls
    must echo back. The returned task is the row only — for dependencies, dependents, subtasks,
    children, context and labels, call `task_show` (`aq task show <id>`), which is the full view.

     Claim a ready task for the calling pool/task session (pull-based work selection, swarm-work-model
    §10). Pass `next: true` for the next available task matching the session's profile, or `task_id` for
    a specific one. `wait` long-polls (clamped to `swarm.claim_wait_max`) instead of returning
    `no_ready_work` immediately. Writes `.aq/claim.json` and returns the task's own row plus
    `claim_epoch`, which subsequent `task_close` / `task_heartbeat` / `task_set` / `task_handoff` calls
    must echo back. The returned task is the row only — for dependencies, dependents, subtasks,
    children, context and labels, call `task_show` (`aq task show <id>`), which is the full view.

    Args:
        body (TaskClaimRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskClaimResponse | TaskClaimResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: TaskClaimRequest,
) -> TaskClaimResponse | TaskClaimResponse422 | None:
    """Claim a ready task for the calling pool/task session (pull-based work selection, swarm-work-model
    §10). Pass `next: true` for the next available task matching the session's profile, or `task_id` for
    a specific one. `wait` long-polls (clamped to `swarm.claim_wait_max`) instead of returning
    `no_ready_work` immediately. Writes `.aq/claim.json` and returns the task's own row plus
    `claim_epoch`, which subsequent `task_close` / `task_heartbeat` / `task_set` / `task_handoff` calls
    must echo back. The returned task is the row only — for dependencies, dependents, subtasks,
    children, context and labels, call `task_show` (`aq task show <id>`), which is the full view.

     Claim a ready task for the calling pool/task session (pull-based work selection, swarm-work-model
    §10). Pass `next: true` for the next available task matching the session's profile, or `task_id` for
    a specific one. `wait` long-polls (clamped to `swarm.claim_wait_max`) instead of returning
    `no_ready_work` immediately. Writes `.aq/claim.json` and returns the task's own row plus
    `claim_epoch`, which subsequent `task_close` / `task_heartbeat` / `task_set` / `task_handoff` calls
    must echo back. The returned task is the row only — for dependencies, dependents, subtasks,
    children, context and labels, call `task_show` (`aq task show <id>`), which is the full view.

    Args:
        body (TaskClaimRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskClaimResponse | TaskClaimResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
