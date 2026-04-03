from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.remove_workspace_request import RemoveWorkspaceRequest
from ...models.remove_workspace_response import RemoveWorkspaceResponse
from ...models.remove_workspace_response_422 import RemoveWorkspaceResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: RemoveWorkspaceRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/project/remove-workspace",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> RemoveWorkspaceResponse | RemoveWorkspaceResponse422 | None:
    if response.status_code == 200:
        response_200 = RemoveWorkspaceResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = RemoveWorkspaceResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[RemoveWorkspaceResponse | RemoveWorkspaceResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: RemoveWorkspaceRequest,
) -> Response[RemoveWorkspaceResponse | RemoveWorkspaceResponse422]:
    """Delete a workspace from a project. The workspace must not be locked by an agent. Accepts a workspace
    ID or name. Does not delete files on disk — only removes the workspace record from the database.

     Delete a workspace from a project. The workspace must not be locked by an agent. Accepts a workspace
    ID or name. Does not delete files on disk — only removes the workspace record from the database.

    Args:
        body (RemoveWorkspaceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[RemoveWorkspaceResponse | RemoveWorkspaceResponse422]
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
    body: RemoveWorkspaceRequest,
) -> RemoveWorkspaceResponse | RemoveWorkspaceResponse422 | None:
    """Delete a workspace from a project. The workspace must not be locked by an agent. Accepts a workspace
    ID or name. Does not delete files on disk — only removes the workspace record from the database.

     Delete a workspace from a project. The workspace must not be locked by an agent. Accepts a workspace
    ID or name. Does not delete files on disk — only removes the workspace record from the database.

    Args:
        body (RemoveWorkspaceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        RemoveWorkspaceResponse | RemoveWorkspaceResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: RemoveWorkspaceRequest,
) -> Response[RemoveWorkspaceResponse | RemoveWorkspaceResponse422]:
    """Delete a workspace from a project. The workspace must not be locked by an agent. Accepts a workspace
    ID or name. Does not delete files on disk — only removes the workspace record from the database.

     Delete a workspace from a project. The workspace must not be locked by an agent. Accepts a workspace
    ID or name. Does not delete files on disk — only removes the workspace record from the database.

    Args:
        body (RemoveWorkspaceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[RemoveWorkspaceResponse | RemoveWorkspaceResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: RemoveWorkspaceRequest,
) -> RemoveWorkspaceResponse | RemoveWorkspaceResponse422 | None:
    """Delete a workspace from a project. The workspace must not be locked by an agent. Accepts a workspace
    ID or name. Does not delete files on disk — only removes the workspace record from the database.

     Delete a workspace from a project. The workspace must not be locked by an agent. Accepts a workspace
    ID or name. Does not delete files on disk — only removes the workspace record from the database.

    Args:
        body (RemoveWorkspaceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        RemoveWorkspaceResponse | RemoveWorkspaceResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
