from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.find_merge_conflict_workspaces_request import FindMergeConflictWorkspacesRequest
from ...models.find_merge_conflict_workspaces_response import FindMergeConflictWorkspacesResponse
from ...models.find_merge_conflict_workspaces_response_422 import FindMergeConflictWorkspacesResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: FindMergeConflictWorkspacesRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/project/find-merge-conflict-workspaces",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> FindMergeConflictWorkspacesResponse | FindMergeConflictWorkspacesResponse422 | None:
    if response.status_code == 200:
        response_200 = FindMergeConflictWorkspacesResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = FindMergeConflictWorkspacesResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[FindMergeConflictWorkspacesResponse | FindMergeConflictWorkspacesResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: FindMergeConflictWorkspacesRequest,
) -> Response[FindMergeConflictWorkspacesResponse | FindMergeConflictWorkspacesResponse422]:
    """Scan project workspaces to find which ones have branches with merge conflicts against the default
    branch (main). Returns workspace IDs, conflicting branches, and file details. Use this BEFORE
    creating a merge-conflict resolution task so you can pass the correct preferred_workspace_id to
    create_task — ensuring the agent gets assigned the workspace that actually contains the conflict.

     Scan project workspaces to find which ones have branches with merge conflicts against the default
    branch (main). Returns workspace IDs, conflicting branches, and file details. Use this BEFORE
    creating a merge-conflict resolution task so you can pass the correct preferred_workspace_id to
    create_task — ensuring the agent gets assigned the workspace that actually contains the conflict.

    Args:
        body (FindMergeConflictWorkspacesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[FindMergeConflictWorkspacesResponse | FindMergeConflictWorkspacesResponse422]
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
    body: FindMergeConflictWorkspacesRequest,
) -> FindMergeConflictWorkspacesResponse | FindMergeConflictWorkspacesResponse422 | None:
    """Scan project workspaces to find which ones have branches with merge conflicts against the default
    branch (main). Returns workspace IDs, conflicting branches, and file details. Use this BEFORE
    creating a merge-conflict resolution task so you can pass the correct preferred_workspace_id to
    create_task — ensuring the agent gets assigned the workspace that actually contains the conflict.

     Scan project workspaces to find which ones have branches with merge conflicts against the default
    branch (main). Returns workspace IDs, conflicting branches, and file details. Use this BEFORE
    creating a merge-conflict resolution task so you can pass the correct preferred_workspace_id to
    create_task — ensuring the agent gets assigned the workspace that actually contains the conflict.

    Args:
        body (FindMergeConflictWorkspacesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        FindMergeConflictWorkspacesResponse | FindMergeConflictWorkspacesResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: FindMergeConflictWorkspacesRequest,
) -> Response[FindMergeConflictWorkspacesResponse | FindMergeConflictWorkspacesResponse422]:
    """Scan project workspaces to find which ones have branches with merge conflicts against the default
    branch (main). Returns workspace IDs, conflicting branches, and file details. Use this BEFORE
    creating a merge-conflict resolution task so you can pass the correct preferred_workspace_id to
    create_task — ensuring the agent gets assigned the workspace that actually contains the conflict.

     Scan project workspaces to find which ones have branches with merge conflicts against the default
    branch (main). Returns workspace IDs, conflicting branches, and file details. Use this BEFORE
    creating a merge-conflict resolution task so you can pass the correct preferred_workspace_id to
    create_task — ensuring the agent gets assigned the workspace that actually contains the conflict.

    Args:
        body (FindMergeConflictWorkspacesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[FindMergeConflictWorkspacesResponse | FindMergeConflictWorkspacesResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: FindMergeConflictWorkspacesRequest,
) -> FindMergeConflictWorkspacesResponse | FindMergeConflictWorkspacesResponse422 | None:
    """Scan project workspaces to find which ones have branches with merge conflicts against the default
    branch (main). Returns workspace IDs, conflicting branches, and file details. Use this BEFORE
    creating a merge-conflict resolution task so you can pass the correct preferred_workspace_id to
    create_task — ensuring the agent gets assigned the workspace that actually contains the conflict.

     Scan project workspaces to find which ones have branches with merge conflicts against the default
    branch (main). Returns workspace IDs, conflicting branches, and file details. Use this BEFORE
    creating a merge-conflict resolution task so you can pass the correct preferred_workspace_id to
    create_task — ensuring the agent gets assigned the workspace that actually contains the conflict.

    Args:
        body (FindMergeConflictWorkspacesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        FindMergeConflictWorkspacesResponse | FindMergeConflictWorkspacesResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
