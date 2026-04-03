from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.queue_sync_workspaces_request import QueueSyncWorkspacesRequest
from ...models.queue_sync_workspaces_response import QueueSyncWorkspacesResponse
from ...models.queue_sync_workspaces_response_422 import QueueSyncWorkspacesResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: QueueSyncWorkspacesRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/project/queue-sync-workspaces",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> QueueSyncWorkspacesResponse | QueueSyncWorkspacesResponse422 | None:
    if response.status_code == 200:
        response_200 = QueueSyncWorkspacesResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = QueueSyncWorkspacesResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[QueueSyncWorkspacesResponse | QueueSyncWorkspacesResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: QueueSyncWorkspacesRequest,
) -> Response[QueueSyncWorkspacesResponse | QueueSyncWorkspacesResponse422]:
    """Queue a high-priority Sync Workspaces task that orchestrates a full workspace synchronization
    workflow. When executed, the task will: (1) pause the project, (2) wait for all active tasks to
    complete, (3) launch a Claude Code agent to merge all feature branches into the default branch
    across all workspaces, (4) resume the project. Use this when workspaces have drifted from the
    default branch and feature work is stuck on feature branches that need consolidation.

     Queue a high-priority Sync Workspaces task that orchestrates a full workspace synchronization
    workflow. When executed, the task will: (1) pause the project, (2) wait for all active tasks to
    complete, (3) launch a Claude Code agent to merge all feature branches into the default branch
    across all workspaces, (4) resume the project. Use this when workspaces have drifted from the
    default branch and feature work is stuck on feature branches that need consolidation.

    Args:
        body (QueueSyncWorkspacesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[QueueSyncWorkspacesResponse | QueueSyncWorkspacesResponse422]
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
    body: QueueSyncWorkspacesRequest,
) -> QueueSyncWorkspacesResponse | QueueSyncWorkspacesResponse422 | None:
    """Queue a high-priority Sync Workspaces task that orchestrates a full workspace synchronization
    workflow. When executed, the task will: (1) pause the project, (2) wait for all active tasks to
    complete, (3) launch a Claude Code agent to merge all feature branches into the default branch
    across all workspaces, (4) resume the project. Use this when workspaces have drifted from the
    default branch and feature work is stuck on feature branches that need consolidation.

     Queue a high-priority Sync Workspaces task that orchestrates a full workspace synchronization
    workflow. When executed, the task will: (1) pause the project, (2) wait for all active tasks to
    complete, (3) launch a Claude Code agent to merge all feature branches into the default branch
    across all workspaces, (4) resume the project. Use this when workspaces have drifted from the
    default branch and feature work is stuck on feature branches that need consolidation.

    Args:
        body (QueueSyncWorkspacesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        QueueSyncWorkspacesResponse | QueueSyncWorkspacesResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: QueueSyncWorkspacesRequest,
) -> Response[QueueSyncWorkspacesResponse | QueueSyncWorkspacesResponse422]:
    """Queue a high-priority Sync Workspaces task that orchestrates a full workspace synchronization
    workflow. When executed, the task will: (1) pause the project, (2) wait for all active tasks to
    complete, (3) launch a Claude Code agent to merge all feature branches into the default branch
    across all workspaces, (4) resume the project. Use this when workspaces have drifted from the
    default branch and feature work is stuck on feature branches that need consolidation.

     Queue a high-priority Sync Workspaces task that orchestrates a full workspace synchronization
    workflow. When executed, the task will: (1) pause the project, (2) wait for all active tasks to
    complete, (3) launch a Claude Code agent to merge all feature branches into the default branch
    across all workspaces, (4) resume the project. Use this when workspaces have drifted from the
    default branch and feature work is stuck on feature branches that need consolidation.

    Args:
        body (QueueSyncWorkspacesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[QueueSyncWorkspacesResponse | QueueSyncWorkspacesResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: QueueSyncWorkspacesRequest,
) -> QueueSyncWorkspacesResponse | QueueSyncWorkspacesResponse422 | None:
    """Queue a high-priority Sync Workspaces task that orchestrates a full workspace synchronization
    workflow. When executed, the task will: (1) pause the project, (2) wait for all active tasks to
    complete, (3) launch a Claude Code agent to merge all feature branches into the default branch
    across all workspaces, (4) resume the project. Use this when workspaces have drifted from the
    default branch and feature work is stuck on feature branches that need consolidation.

     Queue a high-priority Sync Workspaces task that orchestrates a full workspace synchronization
    workflow. When executed, the task will: (1) pause the project, (2) wait for all active tasks to
    complete, (3) launch a Claude Code agent to merge all feature branches into the default branch
    across all workspaces, (4) resume the project. Use this when workspaces have drifted from the
    default branch and feature work is stuck on feature branches that need consolidation.

    Args:
        body (QueueSyncWorkspacesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        QueueSyncWorkspacesResponse | QueueSyncWorkspacesResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
