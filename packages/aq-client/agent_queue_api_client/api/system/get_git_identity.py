from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.get_git_identity_request import GetGitIdentityRequest
from ...models.get_git_identity_response import GetGitIdentityResponse
from ...models.get_git_identity_response_422 import GetGitIdentityResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: GetGitIdentityRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/get-git-identity",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> GetGitIdentityResponse | GetGitIdentityResponse422 | None:
    if response.status_code == 200:
        response_200 = GetGitIdentityResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = GetGitIdentityResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[GetGitIdentityResponse | GetGitIdentityResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetGitIdentityRequest,
) -> Response[GetGitIdentityResponse | GetGitIdentityResponse422]:
    """Show the Git commit identity AQ writes with: the installation default (config git_identity), whether
    it is configured, the documented fallback used while it is unset, and, given project_id, that
    project's effective identity and its source (project override, installation default or fallback).

     Show the Git commit identity AQ writes with: the installation default (config git_identity), whether
    it is configured, the documented fallback used while it is unset, and, given project_id, that
    project's effective identity and its source (project override, installation default or fallback).

    Args:
        body (GetGitIdentityRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetGitIdentityResponse | GetGitIdentityResponse422]
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
    body: GetGitIdentityRequest,
) -> GetGitIdentityResponse | GetGitIdentityResponse422 | None:
    """Show the Git commit identity AQ writes with: the installation default (config git_identity), whether
    it is configured, the documented fallback used while it is unset, and, given project_id, that
    project's effective identity and its source (project override, installation default or fallback).

     Show the Git commit identity AQ writes with: the installation default (config git_identity), whether
    it is configured, the documented fallback used while it is unset, and, given project_id, that
    project's effective identity and its source (project override, installation default or fallback).

    Args:
        body (GetGitIdentityRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetGitIdentityResponse | GetGitIdentityResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetGitIdentityRequest,
) -> Response[GetGitIdentityResponse | GetGitIdentityResponse422]:
    """Show the Git commit identity AQ writes with: the installation default (config git_identity), whether
    it is configured, the documented fallback used while it is unset, and, given project_id, that
    project's effective identity and its source (project override, installation default or fallback).

     Show the Git commit identity AQ writes with: the installation default (config git_identity), whether
    it is configured, the documented fallback used while it is unset, and, given project_id, that
    project's effective identity and its source (project override, installation default or fallback).

    Args:
        body (GetGitIdentityRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetGitIdentityResponse | GetGitIdentityResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: GetGitIdentityRequest,
) -> GetGitIdentityResponse | GetGitIdentityResponse422 | None:
    """Show the Git commit identity AQ writes with: the installation default (config git_identity), whether
    it is configured, the documented fallback used while it is unset, and, given project_id, that
    project's effective identity and its source (project override, installation default or fallback).

     Show the Git commit identity AQ writes with: the installation default (config git_identity), whether
    it is configured, the documented fallback used while it is unset, and, given project_id, that
    project's effective identity and its source (project override, installation default or fallback).

    Args:
        body (GetGitIdentityRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetGitIdentityResponse | GetGitIdentityResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
