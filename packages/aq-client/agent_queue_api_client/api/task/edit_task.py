from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.edit_task_request import EditTaskRequest
from ...models.edit_task_response import EditTaskResponse
from ...models.edit_task_response_422 import EditTaskResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: EditTaskRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/edit",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> EditTaskResponse | EditTaskResponse422 | None:
    if response.status_code == 200:
        response_200 = EditTaskResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = EditTaskResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[EditTaskResponse | EditTaskResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: EditTaskRequest,
) -> Response[EditTaskResponse | EditTaskResponse422]:
    """Edit a task's properties: project_id, title, description, priority, task_type, status, max_retries,
    verification_type, profile_id, or auto_approve_plan. Use this to move a task to a different project,
    rename tasks, change priority, override status (admin), assign a profile, or adjust
    retry/verification settings.

     Edit a task's properties: project_id, title, description, priority, task_type, status, max_retries,
    verification_type, profile_id, or auto_approve_plan. Use this to move a task to a different project,
    rename tasks, change priority, override status (admin), assign a profile, or adjust
    retry/verification settings.

    Args:
        body (EditTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EditTaskResponse | EditTaskResponse422]
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
    body: EditTaskRequest,
) -> EditTaskResponse | EditTaskResponse422 | None:
    """Edit a task's properties: project_id, title, description, priority, task_type, status, max_retries,
    verification_type, profile_id, or auto_approve_plan. Use this to move a task to a different project,
    rename tasks, change priority, override status (admin), assign a profile, or adjust
    retry/verification settings.

     Edit a task's properties: project_id, title, description, priority, task_type, status, max_retries,
    verification_type, profile_id, or auto_approve_plan. Use this to move a task to a different project,
    rename tasks, change priority, override status (admin), assign a profile, or adjust
    retry/verification settings.

    Args:
        body (EditTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EditTaskResponse | EditTaskResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: EditTaskRequest,
) -> Response[EditTaskResponse | EditTaskResponse422]:
    """Edit a task's properties: project_id, title, description, priority, task_type, status, max_retries,
    verification_type, profile_id, or auto_approve_plan. Use this to move a task to a different project,
    rename tasks, change priority, override status (admin), assign a profile, or adjust
    retry/verification settings.

     Edit a task's properties: project_id, title, description, priority, task_type, status, max_retries,
    verification_type, profile_id, or auto_approve_plan. Use this to move a task to a different project,
    rename tasks, change priority, override status (admin), assign a profile, or adjust
    retry/verification settings.

    Args:
        body (EditTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EditTaskResponse | EditTaskResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: EditTaskRequest,
) -> EditTaskResponse | EditTaskResponse422 | None:
    """Edit a task's properties: project_id, title, description, priority, task_type, status, max_retries,
    verification_type, profile_id, or auto_approve_plan. Use this to move a task to a different project,
    rename tasks, change priority, override status (admin), assign a profile, or adjust
    retry/verification settings.

     Edit a task's properties: project_id, title, description, priority, task_type, status, max_retries,
    verification_type, profile_id, or auto_approve_plan. Use this to move a task to a different project,
    rename tasks, change priority, override status (admin), assign a profile, or adjust
    retry/verification settings.

    Args:
        body (EditTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EditTaskResponse | EditTaskResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
