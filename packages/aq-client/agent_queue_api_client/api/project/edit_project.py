from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.edit_project_request import EditProjectRequest
from ...models.edit_project_response import EditProjectResponse
from ...models.edit_project_response_422 import EditProjectResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: EditProjectRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/project/edit",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> EditProjectResponse | EditProjectResponse422 | None:
    if response.status_code == 200:
        response_200 = EditProjectResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = EditProjectResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[EditProjectResponse | EditProjectResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: EditProjectRequest,
) -> Response[EditProjectResponse | EditProjectResponse422]:
    """Edit a project's properties: name, credit_weight, max_concurrent_agents, budget_limit,
    discord_channel_id, default_profile_id, or repo_default_branch. Use this to rename projects, adjust
    scheduling weight, set token budgets, link Discord channels, set a default agent profile, or change
    the default git branch.

     Edit a project's properties: name, credit_weight, max_concurrent_agents, budget_limit,
    discord_channel_id, default_profile_id, or repo_default_branch. Use this to rename projects, adjust
    scheduling weight, set token budgets, link Discord channels, set a default agent profile, or change
    the default git branch.

    Args:
        body (EditProjectRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EditProjectResponse | EditProjectResponse422]
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
    body: EditProjectRequest,
) -> EditProjectResponse | EditProjectResponse422 | None:
    """Edit a project's properties: name, credit_weight, max_concurrent_agents, budget_limit,
    discord_channel_id, default_profile_id, or repo_default_branch. Use this to rename projects, adjust
    scheduling weight, set token budgets, link Discord channels, set a default agent profile, or change
    the default git branch.

     Edit a project's properties: name, credit_weight, max_concurrent_agents, budget_limit,
    discord_channel_id, default_profile_id, or repo_default_branch. Use this to rename projects, adjust
    scheduling weight, set token budgets, link Discord channels, set a default agent profile, or change
    the default git branch.

    Args:
        body (EditProjectRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EditProjectResponse | EditProjectResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: EditProjectRequest,
) -> Response[EditProjectResponse | EditProjectResponse422]:
    """Edit a project's properties: name, credit_weight, max_concurrent_agents, budget_limit,
    discord_channel_id, default_profile_id, or repo_default_branch. Use this to rename projects, adjust
    scheduling weight, set token budgets, link Discord channels, set a default agent profile, or change
    the default git branch.

     Edit a project's properties: name, credit_weight, max_concurrent_agents, budget_limit,
    discord_channel_id, default_profile_id, or repo_default_branch. Use this to rename projects, adjust
    scheduling weight, set token budgets, link Discord channels, set a default agent profile, or change
    the default git branch.

    Args:
        body (EditProjectRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EditProjectResponse | EditProjectResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: EditProjectRequest,
) -> EditProjectResponse | EditProjectResponse422 | None:
    """Edit a project's properties: name, credit_weight, max_concurrent_agents, budget_limit,
    discord_channel_id, default_profile_id, or repo_default_branch. Use this to rename projects, adjust
    scheduling weight, set token budgets, link Discord channels, set a default agent profile, or change
    the default git branch.

     Edit a project's properties: name, credit_weight, max_concurrent_agents, budget_limit,
    discord_channel_id, default_profile_id, or repo_default_branch. Use this to rename projects, adjust
    scheduling weight, set token budgets, link Discord channels, set a default agent profile, or change
    the default git branch.

    Args:
        body (EditProjectRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EditProjectResponse | EditProjectResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
