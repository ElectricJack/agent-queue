from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.archive_tasks_request import ArchiveTasksRequest
from ...models.archive_tasks_response import ArchiveTasksResponse
from ...models.archive_tasks_response_422 import ArchiveTasksResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ArchiveTasksRequest,
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
) -> ArchiveTasksResponse | ArchiveTasksResponse422 | None:
    if response.status_code == 200:
        response_200 = ArchiveTasksResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ArchiveTasksResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ArchiveTasksResponse | ArchiveTasksResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ArchiveTasksRequest,
) -> Response[ArchiveTasksResponse | ArchiveTasksResponse422]:
    """Archive completed tasks to clear them from active task lists. Tasks are moved to the archived_tasks
    DB table (viewable with list_archived, restorable with restore_task) and a markdown reference note
    is written to ~/.agent-queue/archived_tasks/. Optionally also archive FAILED and BLOCKED tasks.

     Archive completed tasks to clear them from active task lists. Tasks are moved to the archived_tasks
    DB table (viewable with list_archived, restorable with restore_task) and a markdown reference note
    is written to ~/.agent-queue/archived_tasks/. Optionally also archive FAILED and BLOCKED tasks.

    Args:
        body (ArchiveTasksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ArchiveTasksResponse | ArchiveTasksResponse422]
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
    body: ArchiveTasksRequest,
) -> ArchiveTasksResponse | ArchiveTasksResponse422 | None:
    """Archive completed tasks to clear them from active task lists. Tasks are moved to the archived_tasks
    DB table (viewable with list_archived, restorable with restore_task) and a markdown reference note
    is written to ~/.agent-queue/archived_tasks/. Optionally also archive FAILED and BLOCKED tasks.

     Archive completed tasks to clear them from active task lists. Tasks are moved to the archived_tasks
    DB table (viewable with list_archived, restorable with restore_task) and a markdown reference note
    is written to ~/.agent-queue/archived_tasks/. Optionally also archive FAILED and BLOCKED tasks.

    Args:
        body (ArchiveTasksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ArchiveTasksResponse | ArchiveTasksResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ArchiveTasksRequest,
) -> Response[ArchiveTasksResponse | ArchiveTasksResponse422]:
    """Archive completed tasks to clear them from active task lists. Tasks are moved to the archived_tasks
    DB table (viewable with list_archived, restorable with restore_task) and a markdown reference note
    is written to ~/.agent-queue/archived_tasks/. Optionally also archive FAILED and BLOCKED tasks.

     Archive completed tasks to clear them from active task lists. Tasks are moved to the archived_tasks
    DB table (viewable with list_archived, restorable with restore_task) and a markdown reference note
    is written to ~/.agent-queue/archived_tasks/. Optionally also archive FAILED and BLOCKED tasks.

    Args:
        body (ArchiveTasksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ArchiveTasksResponse | ArchiveTasksResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ArchiveTasksRequest,
) -> ArchiveTasksResponse | ArchiveTasksResponse422 | None:
    """Archive completed tasks to clear them from active task lists. Tasks are moved to the archived_tasks
    DB table (viewable with list_archived, restorable with restore_task) and a markdown reference note
    is written to ~/.agent-queue/archived_tasks/. Optionally also archive FAILED and BLOCKED tasks.

     Archive completed tasks to clear them from active task lists. Tasks are moved to the archived_tasks
    DB table (viewable with list_archived, restorable with restore_task) and a markdown reference note
    is written to ~/.agent-queue/archived_tasks/. Optionally also archive FAILED and BLOCKED tasks.

    Args:
        body (ArchiveTasksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ArchiveTasksResponse | ArchiveTasksResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
