from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.list_active_tasks_all_projects_request import ListActiveTasksAllProjectsRequest
from ...models.list_active_tasks_all_projects_response import ListActiveTasksAllProjectsResponse
from ...models.list_active_tasks_all_projects_response_422 import ListActiveTasksAllProjectsResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ListActiveTasksAllProjectsRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/list-active-all-projects",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ListActiveTasksAllProjectsResponse | ListActiveTasksAllProjectsResponse422 | None:
    if response.status_code == 200:
        response_200 = ListActiveTasksAllProjectsResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ListActiveTasksAllProjectsResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ListActiveTasksAllProjectsResponse | ListActiveTasksAllProjectsResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListActiveTasksAllProjectsRequest,
) -> Response[ListActiveTasksAllProjectsResponse | ListActiveTasksAllProjectsResponse422]:
    """List active tasks across ALL projects, grouped by project. Returns only non-terminal tasks (excludes
    COMPLETED, FAILED, BLOCKED) by default. Use this when the user wants a cross-project overview of
    everything that is queued, in-progress, or actionable. Response includes 'by_project' (grouped),
    'tasks' (flat list), 'total', 'project_count', and 'hidden_completed' (number of terminal tasks not
    shown). When presenting results, say 'N active tasks across M projects'.

     List active tasks across ALL projects, grouped by project. Returns only non-terminal tasks (excludes
    COMPLETED, FAILED, BLOCKED) by default. Use this when the user wants a cross-project overview of
    everything that is queued, in-progress, or actionable. Response includes 'by_project' (grouped),
    'tasks' (flat list), 'total', 'project_count', and 'hidden_completed' (number of terminal tasks not
    shown). When presenting results, say 'N active tasks across M projects'.

    Args:
        body (ListActiveTasksAllProjectsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListActiveTasksAllProjectsResponse | ListActiveTasksAllProjectsResponse422]
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
    body: ListActiveTasksAllProjectsRequest,
) -> ListActiveTasksAllProjectsResponse | ListActiveTasksAllProjectsResponse422 | None:
    """List active tasks across ALL projects, grouped by project. Returns only non-terminal tasks (excludes
    COMPLETED, FAILED, BLOCKED) by default. Use this when the user wants a cross-project overview of
    everything that is queued, in-progress, or actionable. Response includes 'by_project' (grouped),
    'tasks' (flat list), 'total', 'project_count', and 'hidden_completed' (number of terminal tasks not
    shown). When presenting results, say 'N active tasks across M projects'.

     List active tasks across ALL projects, grouped by project. Returns only non-terminal tasks (excludes
    COMPLETED, FAILED, BLOCKED) by default. Use this when the user wants a cross-project overview of
    everything that is queued, in-progress, or actionable. Response includes 'by_project' (grouped),
    'tasks' (flat list), 'total', 'project_count', and 'hidden_completed' (number of terminal tasks not
    shown). When presenting results, say 'N active tasks across M projects'.

    Args:
        body (ListActiveTasksAllProjectsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListActiveTasksAllProjectsResponse | ListActiveTasksAllProjectsResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListActiveTasksAllProjectsRequest,
) -> Response[ListActiveTasksAllProjectsResponse | ListActiveTasksAllProjectsResponse422]:
    """List active tasks across ALL projects, grouped by project. Returns only non-terminal tasks (excludes
    COMPLETED, FAILED, BLOCKED) by default. Use this when the user wants a cross-project overview of
    everything that is queued, in-progress, or actionable. Response includes 'by_project' (grouped),
    'tasks' (flat list), 'total', 'project_count', and 'hidden_completed' (number of terminal tasks not
    shown). When presenting results, say 'N active tasks across M projects'.

     List active tasks across ALL projects, grouped by project. Returns only non-terminal tasks (excludes
    COMPLETED, FAILED, BLOCKED) by default. Use this when the user wants a cross-project overview of
    everything that is queued, in-progress, or actionable. Response includes 'by_project' (grouped),
    'tasks' (flat list), 'total', 'project_count', and 'hidden_completed' (number of terminal tasks not
    shown). When presenting results, say 'N active tasks across M projects'.

    Args:
        body (ListActiveTasksAllProjectsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListActiveTasksAllProjectsResponse | ListActiveTasksAllProjectsResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ListActiveTasksAllProjectsRequest,
) -> ListActiveTasksAllProjectsResponse | ListActiveTasksAllProjectsResponse422 | None:
    """List active tasks across ALL projects, grouped by project. Returns only non-terminal tasks (excludes
    COMPLETED, FAILED, BLOCKED) by default. Use this when the user wants a cross-project overview of
    everything that is queued, in-progress, or actionable. Response includes 'by_project' (grouped),
    'tasks' (flat list), 'total', 'project_count', and 'hidden_completed' (number of terminal tasks not
    shown). When presenting results, say 'N active tasks across M projects'.

     List active tasks across ALL projects, grouped by project. Returns only non-terminal tasks (excludes
    COMPLETED, FAILED, BLOCKED) by default. Use this when the user wants a cross-project overview of
    everything that is queued, in-progress, or actionable. Response includes 'by_project' (grouped),
    'tasks' (flat list), 'total', 'project_count', and 'hidden_completed' (number of terminal tasks not
    shown). When presenting results, say 'N active tasks across M projects'.

    Args:
        body (ListActiveTasksAllProjectsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListActiveTasksAllProjectsResponse | ListActiveTasksAllProjectsResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
