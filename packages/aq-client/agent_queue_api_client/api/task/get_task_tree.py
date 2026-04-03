from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.get_task_tree_request import GetTaskTreeRequest
from ...models.get_task_tree_response import GetTaskTreeResponse
from ...models.get_task_tree_response_422 import GetTaskTreeResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: GetTaskTreeRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/get-tree",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> GetTaskTreeResponse | GetTaskTreeResponse422 | None:
    if response.status_code == 200:
        response_200 = GetTaskTreeResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = GetTaskTreeResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[GetTaskTreeResponse | GetTaskTreeResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetTaskTreeRequest,
) -> Response[GetTaskTreeResponse | GetTaskTreeResponse422]:
    """Get the subtask hierarchy for a specific parent task, rendered as a tree with box-drawing
    characters. Returns a 'display' field with pre-formatted text showing the full parent->child
    hierarchy, status emojis, and a progress summary. Use this when the user asks about subtasks of a
    specific task or wants to inspect a plan's structure. For a project-wide tree view, use list_tasks
    with display_mode='tree' instead.

     Get the subtask hierarchy for a specific parent task, rendered as a tree with box-drawing
    characters. Returns a 'display' field with pre-formatted text showing the full parent->child
    hierarchy, status emojis, and a progress summary. Use this when the user asks about subtasks of a
    specific task or wants to inspect a plan's structure. For a project-wide tree view, use list_tasks
    with display_mode='tree' instead.

    Args:
        body (GetTaskTreeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetTaskTreeResponse | GetTaskTreeResponse422]
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
    body: GetTaskTreeRequest,
) -> GetTaskTreeResponse | GetTaskTreeResponse422 | None:
    """Get the subtask hierarchy for a specific parent task, rendered as a tree with box-drawing
    characters. Returns a 'display' field with pre-formatted text showing the full parent->child
    hierarchy, status emojis, and a progress summary. Use this when the user asks about subtasks of a
    specific task or wants to inspect a plan's structure. For a project-wide tree view, use list_tasks
    with display_mode='tree' instead.

     Get the subtask hierarchy for a specific parent task, rendered as a tree with box-drawing
    characters. Returns a 'display' field with pre-formatted text showing the full parent->child
    hierarchy, status emojis, and a progress summary. Use this when the user asks about subtasks of a
    specific task or wants to inspect a plan's structure. For a project-wide tree view, use list_tasks
    with display_mode='tree' instead.

    Args:
        body (GetTaskTreeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetTaskTreeResponse | GetTaskTreeResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetTaskTreeRequest,
) -> Response[GetTaskTreeResponse | GetTaskTreeResponse422]:
    """Get the subtask hierarchy for a specific parent task, rendered as a tree with box-drawing
    characters. Returns a 'display' field with pre-formatted text showing the full parent->child
    hierarchy, status emojis, and a progress summary. Use this when the user asks about subtasks of a
    specific task or wants to inspect a plan's structure. For a project-wide tree view, use list_tasks
    with display_mode='tree' instead.

     Get the subtask hierarchy for a specific parent task, rendered as a tree with box-drawing
    characters. Returns a 'display' field with pre-formatted text showing the full parent->child
    hierarchy, status emojis, and a progress summary. Use this when the user asks about subtasks of a
    specific task or wants to inspect a plan's structure. For a project-wide tree view, use list_tasks
    with display_mode='tree' instead.

    Args:
        body (GetTaskTreeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetTaskTreeResponse | GetTaskTreeResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: GetTaskTreeRequest,
) -> GetTaskTreeResponse | GetTaskTreeResponse422 | None:
    """Get the subtask hierarchy for a specific parent task, rendered as a tree with box-drawing
    characters. Returns a 'display' field with pre-formatted text showing the full parent->child
    hierarchy, status emojis, and a progress summary. Use this when the user asks about subtasks of a
    specific task or wants to inspect a plan's structure. For a project-wide tree view, use list_tasks
    with display_mode='tree' instead.

     Get the subtask hierarchy for a specific parent task, rendered as a tree with box-drawing
    characters. Returns a 'display' field with pre-formatted text showing the full parent->child
    hierarchy, status emojis, and a progress summary. Use this when the user asks about subtasks of a
    specific task or wants to inspect a plan's structure. For a project-wide tree view, use list_tasks
    with display_mode='tree' instead.

    Args:
        body (GetTaskTreeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetTaskTreeResponse | GetTaskTreeResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
