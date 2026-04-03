from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.get_task_dependencies_request import GetTaskDependenciesRequest
from ...models.get_task_dependencies_response_422 import GetTaskDependenciesResponse422
from ...models.task_deps_response import TaskDepsResponse
from ...types import Response


def _get_kwargs(
    *,
    body: GetTaskDependenciesRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/get-dependencies",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> GetTaskDependenciesResponse422 | TaskDepsResponse | None:
    if response.status_code == 200:
        response_200 = TaskDepsResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = GetTaskDependenciesResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[GetTaskDependenciesResponse422 | TaskDepsResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetTaskDependenciesRequest,
) -> Response[GetTaskDependenciesResponse422 | TaskDepsResponse]:
    """Get the full dependency graph for a specific task: what it depends on (upstream) and what it blocks
    (downstream). Each entry includes the task's id, title, and current status. Use this when the user
    asks why a task is blocked, what depends on a task, or wants to understand the dependency chain for
    a specific task. Example: 'Task X is blocked because it depends on Y which is still IN_PROGRESS.'

     Get the full dependency graph for a specific task: what it depends on (upstream) and what it blocks
    (downstream). Each entry includes the task's id, title, and current status. Use this when the user
    asks why a task is blocked, what depends on a task, or wants to understand the dependency chain for
    a specific task. Example: 'Task X is blocked because it depends on Y which is still IN_PROGRESS.'

    Args:
        body (GetTaskDependenciesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetTaskDependenciesResponse422 | TaskDepsResponse]
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
    body: GetTaskDependenciesRequest,
) -> GetTaskDependenciesResponse422 | TaskDepsResponse | None:
    """Get the full dependency graph for a specific task: what it depends on (upstream) and what it blocks
    (downstream). Each entry includes the task's id, title, and current status. Use this when the user
    asks why a task is blocked, what depends on a task, or wants to understand the dependency chain for
    a specific task. Example: 'Task X is blocked because it depends on Y which is still IN_PROGRESS.'

     Get the full dependency graph for a specific task: what it depends on (upstream) and what it blocks
    (downstream). Each entry includes the task's id, title, and current status. Use this when the user
    asks why a task is blocked, what depends on a task, or wants to understand the dependency chain for
    a specific task. Example: 'Task X is blocked because it depends on Y which is still IN_PROGRESS.'

    Args:
        body (GetTaskDependenciesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetTaskDependenciesResponse422 | TaskDepsResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetTaskDependenciesRequest,
) -> Response[GetTaskDependenciesResponse422 | TaskDepsResponse]:
    """Get the full dependency graph for a specific task: what it depends on (upstream) and what it blocks
    (downstream). Each entry includes the task's id, title, and current status. Use this when the user
    asks why a task is blocked, what depends on a task, or wants to understand the dependency chain for
    a specific task. Example: 'Task X is blocked because it depends on Y which is still IN_PROGRESS.'

     Get the full dependency graph for a specific task: what it depends on (upstream) and what it blocks
    (downstream). Each entry includes the task's id, title, and current status. Use this when the user
    asks why a task is blocked, what depends on a task, or wants to understand the dependency chain for
    a specific task. Example: 'Task X is blocked because it depends on Y which is still IN_PROGRESS.'

    Args:
        body (GetTaskDependenciesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetTaskDependenciesResponse422 | TaskDepsResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: GetTaskDependenciesRequest,
) -> GetTaskDependenciesResponse422 | TaskDepsResponse | None:
    """Get the full dependency graph for a specific task: what it depends on (upstream) and what it blocks
    (downstream). Each entry includes the task's id, title, and current status. Use this when the user
    asks why a task is blocked, what depends on a task, or wants to understand the dependency chain for
    a specific task. Example: 'Task X is blocked because it depends on Y which is still IN_PROGRESS.'

     Get the full dependency graph for a specific task: what it depends on (upstream) and what it blocks
    (downstream). Each entry includes the task's id, title, and current status. Use this when the user
    asks why a task is blocked, what depends on a task, or wants to understand the dependency chain for
    a specific task. Example: 'Task X is blocked because it depends on Y which is still IN_PROGRESS.'

    Args:
        body (GetTaskDependenciesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetTaskDependenciesResponse422 | TaskDepsResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
