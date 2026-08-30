from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.archive_task_request import ArchiveTaskRequest
from ...models.archive_task_response import ArchiveTaskResponse
from ...models.archive_task_response_422 import ArchiveTaskResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ArchiveTaskRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/archive",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ArchiveTaskResponse | ArchiveTaskResponse422 | None:
    if response.status_code == 200:
        response_200 = ArchiveTaskResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ArchiveTaskResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ArchiveTaskResponse | ArchiveTaskResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ArchiveTaskRequest,
) -> Response[ArchiveTaskResponse | ArchiveTaskResponse422]:
    """Archive tasks. Provide task_id to archive a single task, or project_id to bulk-archive all completed
    tasks in a project.

     Archive tasks. Provide task_id to archive a single task, or project_id to bulk-archive all completed
    tasks in a project.

    Args:
        body (ArchiveTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ArchiveTaskResponse | ArchiveTaskResponse422]
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
    body: ArchiveTaskRequest,
) -> ArchiveTaskResponse | ArchiveTaskResponse422 | None:
    """Archive tasks. Provide task_id to archive a single task, or project_id to bulk-archive all completed
    tasks in a project.

     Archive tasks. Provide task_id to archive a single task, or project_id to bulk-archive all completed
    tasks in a project.

    Args:
        body (ArchiveTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ArchiveTaskResponse | ArchiveTaskResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ArchiveTaskRequest,
) -> Response[ArchiveTaskResponse | ArchiveTaskResponse422]:
    """Archive tasks. Provide task_id to archive a single task, or project_id to bulk-archive all completed
    tasks in a project.

     Archive tasks. Provide task_id to archive a single task, or project_id to bulk-archive all completed
    tasks in a project.

    Args:
        body (ArchiveTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ArchiveTaskResponse | ArchiveTaskResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ArchiveTaskRequest,
) -> ArchiveTaskResponse | ArchiveTaskResponse422 | None:
    """Archive tasks. Provide task_id to archive a single task, or project_id to bulk-archive all completed
    tasks in a project.

     Archive tasks. Provide task_id to archive a single task, or project_id to bulk-archive all completed
    tasks in a project.

    Args:
        body (ArchiveTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ArchiveTaskResponse | ArchiveTaskResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
