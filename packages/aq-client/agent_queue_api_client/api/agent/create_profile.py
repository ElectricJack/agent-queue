from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.create_profile_request import CreateProfileRequest
from ...models.create_profile_response import CreateProfileResponse
from ...models.create_profile_response_422 import CreateProfileResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: CreateProfileRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/agent/create-profile",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> CreateProfileResponse | CreateProfileResponse422 | None:
    if response.status_code == 200:
        response_200 = CreateProfileResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = CreateProfileResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[CreateProfileResponse | CreateProfileResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CreateProfileRequest,
) -> Response[CreateProfileResponse | CreateProfileResponse422]:
    """Create a new agent profile. Profiles configure agents with specific tools, MCP servers, model
    overrides, and system prompt additions. Assign profiles to tasks (profile_id) or set as project
    defaults (default_profile_id).

     Create a new agent profile. Profiles configure agents with specific tools, MCP servers, model
    overrides, and system prompt additions. Assign profiles to tasks (profile_id) or set as project
    defaults (default_profile_id).

    Args:
        body (CreateProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CreateProfileResponse | CreateProfileResponse422]
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
    body: CreateProfileRequest,
) -> CreateProfileResponse | CreateProfileResponse422 | None:
    """Create a new agent profile. Profiles configure agents with specific tools, MCP servers, model
    overrides, and system prompt additions. Assign profiles to tasks (profile_id) or set as project
    defaults (default_profile_id).

     Create a new agent profile. Profiles configure agents with specific tools, MCP servers, model
    overrides, and system prompt additions. Assign profiles to tasks (profile_id) or set as project
    defaults (default_profile_id).

    Args:
        body (CreateProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CreateProfileResponse | CreateProfileResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CreateProfileRequest,
) -> Response[CreateProfileResponse | CreateProfileResponse422]:
    """Create a new agent profile. Profiles configure agents with specific tools, MCP servers, model
    overrides, and system prompt additions. Assign profiles to tasks (profile_id) or set as project
    defaults (default_profile_id).

     Create a new agent profile. Profiles configure agents with specific tools, MCP servers, model
    overrides, and system prompt additions. Assign profiles to tasks (profile_id) or set as project
    defaults (default_profile_id).

    Args:
        body (CreateProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CreateProfileResponse | CreateProfileResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: CreateProfileRequest,
) -> CreateProfileResponse | CreateProfileResponse422 | None:
    """Create a new agent profile. Profiles configure agents with specific tools, MCP servers, model
    overrides, and system prompt additions. Assign profiles to tasks (profile_id) or set as project
    defaults (default_profile_id).

     Create a new agent profile. Profiles configure agents with specific tools, MCP servers, model
    overrides, and system prompt additions. Assign profiles to tasks (profile_id) or set as project
    defaults (default_profile_id).

    Args:
        body (CreateProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CreateProfileResponse | CreateProfileResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
