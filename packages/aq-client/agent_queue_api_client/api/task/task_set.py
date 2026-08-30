from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.task_set_request import TaskSetRequest
from ...models.task_set_response_422 import TaskSetResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: TaskSetRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/set",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | TaskSetResponse422 | None:
    if response.status_code == 200:
        response_200 = response.json()
        return response_200

    if response.status_code == 422:
        response_422 = TaskSetResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Any | TaskSetResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskSetRequest,
) -> Response[Any | TaskSetResponse422]:
    """Write work-state fields on a task and return the updated task: branch, PR URL, work_dir, a note,
    label add/remove, and arbitrary metadata. Never performs a status transition (the state machine
    belongs to work-graph's task_close — use the lifecycle commands for that).  Returns 'fields_changed'
    listing what was written; a call with no recognised field is an error rather than a no-op.  Backs
    `aq task set`.

     Write work-state fields on a task and return the updated task: branch, PR URL, work_dir, a note,
    label add/remove, and arbitrary metadata. Never performs a status transition (the state machine
    belongs to work-graph's task_close — use the lifecycle commands for that).  Returns 'fields_changed'
    listing what was written; a call with no recognised field is an error rather than a no-op.  Backs
    `aq task set`.

    Args:
        body (TaskSetRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | TaskSetResponse422]
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
    body: TaskSetRequest,
) -> Any | TaskSetResponse422 | None:
    """Write work-state fields on a task and return the updated task: branch, PR URL, work_dir, a note,
    label add/remove, and arbitrary metadata. Never performs a status transition (the state machine
    belongs to work-graph's task_close — use the lifecycle commands for that).  Returns 'fields_changed'
    listing what was written; a call with no recognised field is an error rather than a no-op.  Backs
    `aq task set`.

     Write work-state fields on a task and return the updated task: branch, PR URL, work_dir, a note,
    label add/remove, and arbitrary metadata. Never performs a status transition (the state machine
    belongs to work-graph's task_close — use the lifecycle commands for that).  Returns 'fields_changed'
    listing what was written; a call with no recognised field is an error rather than a no-op.  Backs
    `aq task set`.

    Args:
        body (TaskSetRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | TaskSetResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskSetRequest,
) -> Response[Any | TaskSetResponse422]:
    """Write work-state fields on a task and return the updated task: branch, PR URL, work_dir, a note,
    label add/remove, and arbitrary metadata. Never performs a status transition (the state machine
    belongs to work-graph's task_close — use the lifecycle commands for that).  Returns 'fields_changed'
    listing what was written; a call with no recognised field is an error rather than a no-op.  Backs
    `aq task set`.

     Write work-state fields on a task and return the updated task: branch, PR URL, work_dir, a note,
    label add/remove, and arbitrary metadata. Never performs a status transition (the state machine
    belongs to work-graph's task_close — use the lifecycle commands for that).  Returns 'fields_changed'
    listing what was written; a call with no recognised field is an error rather than a no-op.  Backs
    `aq task set`.

    Args:
        body (TaskSetRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | TaskSetResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: TaskSetRequest,
) -> Any | TaskSetResponse422 | None:
    """Write work-state fields on a task and return the updated task: branch, PR URL, work_dir, a note,
    label add/remove, and arbitrary metadata. Never performs a status transition (the state machine
    belongs to work-graph's task_close — use the lifecycle commands for that).  Returns 'fields_changed'
    listing what was written; a call with no recognised field is an error rather than a no-op.  Backs
    `aq task set`.

     Write work-state fields on a task and return the updated task: branch, PR URL, work_dir, a note,
    label add/remove, and arbitrary metadata. Never performs a status transition (the state machine
    belongs to work-graph's task_close — use the lifecycle commands for that).  Returns 'fields_changed'
    listing what was written; a call with no recognised field is an error rather than a no-op.  Backs
    `aq task set`.

    Args:
        body (TaskSetRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | TaskSetResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
