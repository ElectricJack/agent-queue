from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.add_workspace_request import AddWorkspaceRequest
from ...models.add_workspace_response import AddWorkspaceResponse
from ...models.add_workspace_response_422 import AddWorkspaceResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: AddWorkspaceRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/project/add-workspace",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> AddWorkspaceResponse | AddWorkspaceResponse422 | None:
    if response.status_code == 200:
        response_200 = AddWorkspaceResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = AddWorkspaceResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[AddWorkspaceResponse | AddWorkspaceResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: AddWorkspaceRequest,
) -> Response[AddWorkspaceResponse | AddWorkspaceResponse422]:
    """Add a workspace directory for a project. Source types: 'clone' (auto-clones from the project's
    repo_url), 'link' (link an existing directory on disk). Workspaces are project-scoped and
    dynamically acquired by agents when assigned tasks.

     Add a workspace directory for a project. Source types: 'clone' (auto-clones from the project's
    repo_url), 'link' (link an existing directory on disk). Workspaces are project-scoped and
    dynamically acquired by agents when assigned tasks.

    Args:
        body (AddWorkspaceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[AddWorkspaceResponse | AddWorkspaceResponse422]
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
    body: AddWorkspaceRequest,
) -> AddWorkspaceResponse | AddWorkspaceResponse422 | None:
    """Add a workspace directory for a project. Source types: 'clone' (auto-clones from the project's
    repo_url), 'link' (link an existing directory on disk). Workspaces are project-scoped and
    dynamically acquired by agents when assigned tasks.

     Add a workspace directory for a project. Source types: 'clone' (auto-clones from the project's
    repo_url), 'link' (link an existing directory on disk). Workspaces are project-scoped and
    dynamically acquired by agents when assigned tasks.

    Args:
        body (AddWorkspaceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        AddWorkspaceResponse | AddWorkspaceResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: AddWorkspaceRequest,
) -> Response[AddWorkspaceResponse | AddWorkspaceResponse422]:
    """Add a workspace directory for a project. Source types: 'clone' (auto-clones from the project's
    repo_url), 'link' (link an existing directory on disk). Workspaces are project-scoped and
    dynamically acquired by agents when assigned tasks.

     Add a workspace directory for a project. Source types: 'clone' (auto-clones from the project's
    repo_url), 'link' (link an existing directory on disk). Workspaces are project-scoped and
    dynamically acquired by agents when assigned tasks.

    Args:
        body (AddWorkspaceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[AddWorkspaceResponse | AddWorkspaceResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: AddWorkspaceRequest,
) -> AddWorkspaceResponse | AddWorkspaceResponse422 | None:
    """Add a workspace directory for a project. Source types: 'clone' (auto-clones from the project's
    repo_url), 'link' (link an existing directory on disk). Workspaces are project-scoped and
    dynamically acquired by agents when assigned tasks.

     Add a workspace directory for a project. Source types: 'clone' (auto-clones from the project's
    repo_url), 'link' (link an existing directory on disk). Workspaces are project-scoped and
    dynamically acquired by agents when assigned tasks.

    Args:
        body (AddWorkspaceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        AddWorkspaceResponse | AddWorkspaceResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
