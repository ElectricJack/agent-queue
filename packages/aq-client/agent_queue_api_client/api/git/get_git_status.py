from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.get_git_status_request import GetGitStatusRequest
from ...models.get_git_status_response import GetGitStatusResponse
from ...models.get_git_status_response_422 import GetGitStatusResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: GetGitStatusRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/git/get-status",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> GetGitStatusResponse | GetGitStatusResponse422 | None:
    if response.status_code == 200:
        response_200 = GetGitStatusResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = GetGitStatusResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[GetGitStatusResponse | GetGitStatusResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetGitStatusRequest,
) -> Response[GetGitStatusResponse | GetGitStatusResponse422]:
    """Get the git status of a project's repository. Shows current branch, working tree status, and recent
    commits. Reports status for all workspaces registered to the project, or falls back to the project
    workspace path. Operates on the active project's repository.

     Get the git status of a project's repository. Shows current branch, working tree status, and recent
    commits. Reports status for all workspaces registered to the project, or falls back to the project
    workspace path. Operates on the active project's repository.

    Args:
        body (GetGitStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetGitStatusResponse | GetGitStatusResponse422]
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
    body: GetGitStatusRequest,
) -> GetGitStatusResponse | GetGitStatusResponse422 | None:
    """Get the git status of a project's repository. Shows current branch, working tree status, and recent
    commits. Reports status for all workspaces registered to the project, or falls back to the project
    workspace path. Operates on the active project's repository.

     Get the git status of a project's repository. Shows current branch, working tree status, and recent
    commits. Reports status for all workspaces registered to the project, or falls back to the project
    workspace path. Operates on the active project's repository.

    Args:
        body (GetGitStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetGitStatusResponse | GetGitStatusResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetGitStatusRequest,
) -> Response[GetGitStatusResponse | GetGitStatusResponse422]:
    """Get the git status of a project's repository. Shows current branch, working tree status, and recent
    commits. Reports status for all workspaces registered to the project, or falls back to the project
    workspace path. Operates on the active project's repository.

     Get the git status of a project's repository. Shows current branch, working tree status, and recent
    commits. Reports status for all workspaces registered to the project, or falls back to the project
    workspace path. Operates on the active project's repository.

    Args:
        body (GetGitStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetGitStatusResponse | GetGitStatusResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: GetGitStatusRequest,
) -> GetGitStatusResponse | GetGitStatusResponse422 | None:
    """Get the git status of a project's repository. Shows current branch, working tree status, and recent
    commits. Reports status for all workspaces registered to the project, or falls back to the project
    workspace path. Operates on the active project's repository.

     Get the git status of a project's repository. Shows current branch, working tree status, and recent
    commits. Reports status for all workspaces registered to the project, or falls back to the project
    workspace path. Operates on the active project's repository.

    Args:
        body (GetGitStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetGitStatusResponse | GetGitStatusResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
