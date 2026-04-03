from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.list_tasks_request import ListTasksRequest
from ...models.list_tasks_response import ListTasksResponse
from ...models.list_tasks_response_422 import ListTasksResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ListTasksRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/list",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ListTasksResponse | ListTasksResponse422 | None:
    if response.status_code == 200:
        response_200 = ListTasksResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ListTasksResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ListTasksResponse | ListTasksResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListTasksRequest,
) -> Response[ListTasksResponse | ListTasksResponse422]:
    """List tasks, optionally filtered by project or status. IMPORTANT: By default,
    completed/failed/blocked tasks are HIDDEN and only active tasks are returned. When presenting
    results from the default filter, say 'N active tasks' (not just 'N tasks'). Use show_all=true or
    include_completed=true to include finished tasks. Use completed_only=true to see only finished
    tasks. An explicit status filter overrides all convenience flags. Supports three display modes:
    'flat' (default list), 'tree' (hierarchical view with subtasks), and 'compact' (root tasks with
    progress bars). Tree and compact modes require project_id. Use show_dependencies=true to annotate
    each task with its upstream depends_on and downstream blocks relationships — useful for
    understanding blocking chains.

     List tasks, optionally filtered by project or status. IMPORTANT: By default,
    completed/failed/blocked tasks are HIDDEN and only active tasks are returned. When presenting
    results from the default filter, say 'N active tasks' (not just 'N tasks'). Use show_all=true or
    include_completed=true to include finished tasks. Use completed_only=true to see only finished
    tasks. An explicit status filter overrides all convenience flags. Supports three display modes:
    'flat' (default list), 'tree' (hierarchical view with subtasks), and 'compact' (root tasks with
    progress bars). Tree and compact modes require project_id. Use show_dependencies=true to annotate
    each task with its upstream depends_on and downstream blocks relationships — useful for
    understanding blocking chains.

    Args:
        body (ListTasksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListTasksResponse | ListTasksResponse422]
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
    body: ListTasksRequest,
) -> ListTasksResponse | ListTasksResponse422 | None:
    """List tasks, optionally filtered by project or status. IMPORTANT: By default,
    completed/failed/blocked tasks are HIDDEN and only active tasks are returned. When presenting
    results from the default filter, say 'N active tasks' (not just 'N tasks'). Use show_all=true or
    include_completed=true to include finished tasks. Use completed_only=true to see only finished
    tasks. An explicit status filter overrides all convenience flags. Supports three display modes:
    'flat' (default list), 'tree' (hierarchical view with subtasks), and 'compact' (root tasks with
    progress bars). Tree and compact modes require project_id. Use show_dependencies=true to annotate
    each task with its upstream depends_on and downstream blocks relationships — useful for
    understanding blocking chains.

     List tasks, optionally filtered by project or status. IMPORTANT: By default,
    completed/failed/blocked tasks are HIDDEN and only active tasks are returned. When presenting
    results from the default filter, say 'N active tasks' (not just 'N tasks'). Use show_all=true or
    include_completed=true to include finished tasks. Use completed_only=true to see only finished
    tasks. An explicit status filter overrides all convenience flags. Supports three display modes:
    'flat' (default list), 'tree' (hierarchical view with subtasks), and 'compact' (root tasks with
    progress bars). Tree and compact modes require project_id. Use show_dependencies=true to annotate
    each task with its upstream depends_on and downstream blocks relationships — useful for
    understanding blocking chains.

    Args:
        body (ListTasksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListTasksResponse | ListTasksResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListTasksRequest,
) -> Response[ListTasksResponse | ListTasksResponse422]:
    """List tasks, optionally filtered by project or status. IMPORTANT: By default,
    completed/failed/blocked tasks are HIDDEN and only active tasks are returned. When presenting
    results from the default filter, say 'N active tasks' (not just 'N tasks'). Use show_all=true or
    include_completed=true to include finished tasks. Use completed_only=true to see only finished
    tasks. An explicit status filter overrides all convenience flags. Supports three display modes:
    'flat' (default list), 'tree' (hierarchical view with subtasks), and 'compact' (root tasks with
    progress bars). Tree and compact modes require project_id. Use show_dependencies=true to annotate
    each task with its upstream depends_on and downstream blocks relationships — useful for
    understanding blocking chains.

     List tasks, optionally filtered by project or status. IMPORTANT: By default,
    completed/failed/blocked tasks are HIDDEN and only active tasks are returned. When presenting
    results from the default filter, say 'N active tasks' (not just 'N tasks'). Use show_all=true or
    include_completed=true to include finished tasks. Use completed_only=true to see only finished
    tasks. An explicit status filter overrides all convenience flags. Supports three display modes:
    'flat' (default list), 'tree' (hierarchical view with subtasks), and 'compact' (root tasks with
    progress bars). Tree and compact modes require project_id. Use show_dependencies=true to annotate
    each task with its upstream depends_on and downstream blocks relationships — useful for
    understanding blocking chains.

    Args:
        body (ListTasksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListTasksResponse | ListTasksResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ListTasksRequest,
) -> ListTasksResponse | ListTasksResponse422 | None:
    """List tasks, optionally filtered by project or status. IMPORTANT: By default,
    completed/failed/blocked tasks are HIDDEN and only active tasks are returned. When presenting
    results from the default filter, say 'N active tasks' (not just 'N tasks'). Use show_all=true or
    include_completed=true to include finished tasks. Use completed_only=true to see only finished
    tasks. An explicit status filter overrides all convenience flags. Supports three display modes:
    'flat' (default list), 'tree' (hierarchical view with subtasks), and 'compact' (root tasks with
    progress bars). Tree and compact modes require project_id. Use show_dependencies=true to annotate
    each task with its upstream depends_on and downstream blocks relationships — useful for
    understanding blocking chains.

     List tasks, optionally filtered by project or status. IMPORTANT: By default,
    completed/failed/blocked tasks are HIDDEN and only active tasks are returned. When presenting
    results from the default filter, say 'N active tasks' (not just 'N tasks'). Use show_all=true or
    include_completed=true to include finished tasks. Use completed_only=true to see only finished
    tasks. An explicit status filter overrides all convenience flags. Supports three display modes:
    'flat' (default list), 'tree' (hierarchical view with subtasks), and 'compact' (root tasks with
    progress bars). Tree and compact modes require project_id. Use show_dependencies=true to annotate
    each task with its upstream depends_on and downstream blocks relationships — useful for
    understanding blocking chains.

    Args:
        body (ListTasksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListTasksResponse | ListTasksResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
