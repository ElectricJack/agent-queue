from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.get_github_auth_status_request import GetGithubAuthStatusRequest
from ...models.get_github_auth_status_response_422 import GetGithubAuthStatusResponse422
from ...models.github_auth_status_response import GithubAuthStatusResponse
from ...types import Response


def _get_kwargs(
    *,
    body: GetGithubAuthStatusRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/project/get-github-auth-status",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> GetGithubAuthStatusResponse422 | GithubAuthStatusResponse | None:
    if response.status_code == 200:
        response_200 = GithubAuthStatusResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = GetGithubAuthStatusResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[GetGithubAuthStatusResponse422 | GithubAuthStatusResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetGithubAuthStatusRequest,
) -> Response[GetGithubAuthStatusResponse422 | GithubAuthStatusResponse]:
    """Report whether the daemon host's `gh` CLI is installed and authenticated. Never returns credentials.

     Report whether the daemon host's `gh` CLI is installed and authenticated. Never returns credentials.

    Args:
        body (GetGithubAuthStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetGithubAuthStatusResponse422 | GithubAuthStatusResponse]
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
    body: GetGithubAuthStatusRequest,
) -> GetGithubAuthStatusResponse422 | GithubAuthStatusResponse | None:
    """Report whether the daemon host's `gh` CLI is installed and authenticated. Never returns credentials.

     Report whether the daemon host's `gh` CLI is installed and authenticated. Never returns credentials.

    Args:
        body (GetGithubAuthStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetGithubAuthStatusResponse422 | GithubAuthStatusResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetGithubAuthStatusRequest,
) -> Response[GetGithubAuthStatusResponse422 | GithubAuthStatusResponse]:
    """Report whether the daemon host's `gh` CLI is installed and authenticated. Never returns credentials.

     Report whether the daemon host's `gh` CLI is installed and authenticated. Never returns credentials.

    Args:
        body (GetGithubAuthStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetGithubAuthStatusResponse422 | GithubAuthStatusResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: GetGithubAuthStatusRequest,
) -> GetGithubAuthStatusResponse422 | GithubAuthStatusResponse | None:
    """Report whether the daemon host's `gh` CLI is installed and authenticated. Never returns credentials.

     Report whether the daemon host's `gh` CLI is installed and authenticated. Never returns credentials.

    Args:
        body (GetGithubAuthStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetGithubAuthStatusResponse422 | GithubAuthStatusResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
