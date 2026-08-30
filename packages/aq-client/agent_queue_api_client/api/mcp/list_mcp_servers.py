from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.list_mcp_servers_request import ListMcpServersRequest
from ...models.list_mcp_servers_response import ListMcpServersResponse
from ...models.list_mcp_servers_response_422 import ListMcpServersResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ListMcpServersRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/mcp/list-servers",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ListMcpServersResponse | ListMcpServersResponse422 | None:
    if response.status_code == 200:
        response_200 = ListMcpServersResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ListMcpServersResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ListMcpServersResponse | ListMcpServersResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListMcpServersRequest,
) -> Response[ListMcpServersResponse | ListMcpServersResponse422]:
    """List MCP servers visible to a scope.  Omit project_id for system scope; supply it to include
    project-scoped servers and inherited system entries.

     List MCP servers visible to a scope.  Omit project_id for system scope; supply it to include
    project-scoped servers and inherited system entries.

    Args:
        body (ListMcpServersRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListMcpServersResponse | ListMcpServersResponse422]
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
    body: ListMcpServersRequest,
) -> ListMcpServersResponse | ListMcpServersResponse422 | None:
    """List MCP servers visible to a scope.  Omit project_id for system scope; supply it to include
    project-scoped servers and inherited system entries.

     List MCP servers visible to a scope.  Omit project_id for system scope; supply it to include
    project-scoped servers and inherited system entries.

    Args:
        body (ListMcpServersRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListMcpServersResponse | ListMcpServersResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListMcpServersRequest,
) -> Response[ListMcpServersResponse | ListMcpServersResponse422]:
    """List MCP servers visible to a scope.  Omit project_id for system scope; supply it to include
    project-scoped servers and inherited system entries.

     List MCP servers visible to a scope.  Omit project_id for system scope; supply it to include
    project-scoped servers and inherited system entries.

    Args:
        body (ListMcpServersRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListMcpServersResponse | ListMcpServersResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ListMcpServersRequest,
) -> ListMcpServersResponse | ListMcpServersResponse422 | None:
    """List MCP servers visible to a scope.  Omit project_id for system scope; supply it to include
    project-scoped servers and inherited system entries.

     List MCP servers visible to a scope.  Omit project_id for system scope; supply it to include
    project-scoped servers and inherited system entries.

    Args:
        body (ListMcpServersRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListMcpServersResponse | ListMcpServersResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
