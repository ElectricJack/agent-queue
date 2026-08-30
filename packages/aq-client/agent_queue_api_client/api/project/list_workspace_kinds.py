from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.list_workspace_kinds_request import ListWorkspaceKindsRequest
from ...models.list_workspace_kinds_response_422 import ListWorkspaceKindsResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ListWorkspaceKindsRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/project/list-workspace-kinds",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | ListWorkspaceKindsResponse422 | None:
    if response.status_code == 200:
        response_200 = response.json()
        return response_200

    if response.status_code == 422:
        response_422 = ListWorkspaceKindsResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Any | ListWorkspaceKindsResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListWorkspaceKindsRequest,
) -> Response[Any | ListWorkspaceKindsResponse422]:
    """List workspace kinds visible to a project (system + project-scoped overrides). Workspaces-v2 spec
    §10. Each kind defines capability flags (writable, lockable, is_git_repo, auto_attach) used by
    acquisition. Kinds are authored as markdown in vault/[projects/<pid>/]workspace-kinds/<id>.md.

     List workspace kinds visible to a project (system + project-scoped overrides). Workspaces-v2 spec
    §10. Each kind defines capability flags (writable, lockable, is_git_repo, auto_attach) used by
    acquisition. Kinds are authored as markdown in vault/[projects/<pid>/]workspace-kinds/<id>.md.

    Args:
        body (ListWorkspaceKindsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ListWorkspaceKindsResponse422]
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
    body: ListWorkspaceKindsRequest,
) -> Any | ListWorkspaceKindsResponse422 | None:
    """List workspace kinds visible to a project (system + project-scoped overrides). Workspaces-v2 spec
    §10. Each kind defines capability flags (writable, lockable, is_git_repo, auto_attach) used by
    acquisition. Kinds are authored as markdown in vault/[projects/<pid>/]workspace-kinds/<id>.md.

     List workspace kinds visible to a project (system + project-scoped overrides). Workspaces-v2 spec
    §10. Each kind defines capability flags (writable, lockable, is_git_repo, auto_attach) used by
    acquisition. Kinds are authored as markdown in vault/[projects/<pid>/]workspace-kinds/<id>.md.

    Args:
        body (ListWorkspaceKindsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ListWorkspaceKindsResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListWorkspaceKindsRequest,
) -> Response[Any | ListWorkspaceKindsResponse422]:
    """List workspace kinds visible to a project (system + project-scoped overrides). Workspaces-v2 spec
    §10. Each kind defines capability flags (writable, lockable, is_git_repo, auto_attach) used by
    acquisition. Kinds are authored as markdown in vault/[projects/<pid>/]workspace-kinds/<id>.md.

     List workspace kinds visible to a project (system + project-scoped overrides). Workspaces-v2 spec
    §10. Each kind defines capability flags (writable, lockable, is_git_repo, auto_attach) used by
    acquisition. Kinds are authored as markdown in vault/[projects/<pid>/]workspace-kinds/<id>.md.

    Args:
        body (ListWorkspaceKindsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ListWorkspaceKindsResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ListWorkspaceKindsRequest,
) -> Any | ListWorkspaceKindsResponse422 | None:
    """List workspace kinds visible to a project (system + project-scoped overrides). Workspaces-v2 spec
    §10. Each kind defines capability flags (writable, lockable, is_git_repo, auto_attach) used by
    acquisition. Kinds are authored as markdown in vault/[projects/<pid>/]workspace-kinds/<id>.md.

     List workspace kinds visible to a project (system + project-scoped overrides). Workspaces-v2 spec
    §10. Each kind defines capability flags (writable, lockable, is_git_repo, auto_attach) used by
    acquisition. Kinds are authored as markdown in vault/[projects/<pid>/]workspace-kinds/<id>.md.

    Args:
        body (ListWorkspaceKindsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ListWorkspaceKindsResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
