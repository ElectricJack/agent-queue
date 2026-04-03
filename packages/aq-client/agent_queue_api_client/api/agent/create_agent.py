from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.create_agent_request import CreateAgentRequest
from ...models.create_agent_response_422 import CreateAgentResponse422
from ...models.list_agents_response import ListAgentsResponse
from ...types import Response


def _get_kwargs(
    *,
    body: CreateAgentRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/agent/create",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> CreateAgentResponse422 | ListAgentsResponse | None:
    if response.status_code == 200:
        response_200 = ListAgentsResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = CreateAgentResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[CreateAgentResponse422 | ListAgentsResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CreateAgentRequest,
) -> Response[CreateAgentResponse422 | ListAgentsResponse]:
    """Deprecated — agents are now derived from workspaces. Use add_workspace to add agent capacity to a
    project.

     Deprecated — agents are now derived from workspaces. Use add_workspace to add agent capacity to a
    project.

    Args:
        body (CreateAgentRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CreateAgentResponse422 | ListAgentsResponse]
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
    body: CreateAgentRequest,
) -> CreateAgentResponse422 | ListAgentsResponse | None:
    """Deprecated — agents are now derived from workspaces. Use add_workspace to add agent capacity to a
    project.

     Deprecated — agents are now derived from workspaces. Use add_workspace to add agent capacity to a
    project.

    Args:
        body (CreateAgentRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CreateAgentResponse422 | ListAgentsResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CreateAgentRequest,
) -> Response[CreateAgentResponse422 | ListAgentsResponse]:
    """Deprecated — agents are now derived from workspaces. Use add_workspace to add agent capacity to a
    project.

     Deprecated — agents are now derived from workspaces. Use add_workspace to add agent capacity to a
    project.

    Args:
        body (CreateAgentRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CreateAgentResponse422 | ListAgentsResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: CreateAgentRequest,
) -> CreateAgentResponse422 | ListAgentsResponse | None:
    """Deprecated — agents are now derived from workspaces. Use add_workspace to add agent capacity to a
    project.

     Deprecated — agents are now derived from workspaces. Use add_workspace to add agent capacity to a
    project.

    Args:
        body (CreateAgentRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CreateAgentResponse422 | ListAgentsResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
