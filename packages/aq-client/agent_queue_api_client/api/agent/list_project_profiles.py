from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.list_project_profiles_request import ListProjectProfilesRequest
from ...models.list_project_profiles_response import ListProjectProfilesResponse
from ...models.list_project_profiles_response_422 import ListProjectProfilesResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ListProjectProfilesRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/agent/list-project-profiles",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ListProjectProfilesResponse | ListProjectProfilesResponse422 | None:
    if response.status_code == 200:
        response_200 = ListProjectProfilesResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ListProjectProfilesResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ListProjectProfilesResponse | ListProjectProfilesResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListProjectProfilesRequest,
) -> Response[ListProjectProfilesResponse | ListProjectProfilesResponse422]:
    """List per-agent-type profile rows for a project, including the global, scoped, and effective views —
    plus the project-scoped MCP tool catalog snapshot in the same response.

     List per-agent-type profile rows for a project, including the global, scoped, and effective views —
    plus the project-scoped MCP tool catalog snapshot in the same response.

    Args:
        body (ListProjectProfilesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListProjectProfilesResponse | ListProjectProfilesResponse422]
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
    body: ListProjectProfilesRequest,
) -> ListProjectProfilesResponse | ListProjectProfilesResponse422 | None:
    """List per-agent-type profile rows for a project, including the global, scoped, and effective views —
    plus the project-scoped MCP tool catalog snapshot in the same response.

     List per-agent-type profile rows for a project, including the global, scoped, and effective views —
    plus the project-scoped MCP tool catalog snapshot in the same response.

    Args:
        body (ListProjectProfilesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListProjectProfilesResponse | ListProjectProfilesResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListProjectProfilesRequest,
) -> Response[ListProjectProfilesResponse | ListProjectProfilesResponse422]:
    """List per-agent-type profile rows for a project, including the global, scoped, and effective views —
    plus the project-scoped MCP tool catalog snapshot in the same response.

     List per-agent-type profile rows for a project, including the global, scoped, and effective views —
    plus the project-scoped MCP tool catalog snapshot in the same response.

    Args:
        body (ListProjectProfilesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListProjectProfilesResponse | ListProjectProfilesResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ListProjectProfilesRequest,
) -> ListProjectProfilesResponse | ListProjectProfilesResponse422 | None:
    """List per-agent-type profile rows for a project, including the global, scoped, and effective views —
    plus the project-scoped MCP tool catalog snapshot in the same response.

     List per-agent-type profile rows for a project, including the global, scoped, and effective views —
    plus the project-scoped MCP tool catalog snapshot in the same response.

    Args:
        body (ListProjectProfilesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListProjectProfilesResponse | ListProjectProfilesResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
