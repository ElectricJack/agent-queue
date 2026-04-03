from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.create_github_repo_request import CreateGithubRepoRequest
from ...models.create_github_repo_response import CreateGithubRepoResponse
from ...models.create_github_repo_response_422 import CreateGithubRepoResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: CreateGithubRepoRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/git/create-github-repo",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> CreateGithubRepoResponse | CreateGithubRepoResponse422 | None:
    if response.status_code == 200:
        response_200 = CreateGithubRepoResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = CreateGithubRepoResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[CreateGithubRepoResponse | CreateGithubRepoResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CreateGithubRepoRequest,
) -> Response[CreateGithubRepoResponse | CreateGithubRepoResponse422]:
    """Create a new GitHub repository via the gh CLI. Supports private/public repos, org or personal.

     Create a new GitHub repository via the gh CLI. Supports private/public repos, org or personal.

    Args:
        body (CreateGithubRepoRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CreateGithubRepoResponse | CreateGithubRepoResponse422]
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
    body: CreateGithubRepoRequest,
) -> CreateGithubRepoResponse | CreateGithubRepoResponse422 | None:
    """Create a new GitHub repository via the gh CLI. Supports private/public repos, org or personal.

     Create a new GitHub repository via the gh CLI. Supports private/public repos, org or personal.

    Args:
        body (CreateGithubRepoRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CreateGithubRepoResponse | CreateGithubRepoResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CreateGithubRepoRequest,
) -> Response[CreateGithubRepoResponse | CreateGithubRepoResponse422]:
    """Create a new GitHub repository via the gh CLI. Supports private/public repos, org or personal.

     Create a new GitHub repository via the gh CLI. Supports private/public repos, org or personal.

    Args:
        body (CreateGithubRepoRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CreateGithubRepoResponse | CreateGithubRepoResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: CreateGithubRepoRequest,
) -> CreateGithubRepoResponse | CreateGithubRepoResponse422 | None:
    """Create a new GitHub repository via the gh CLI. Supports private/public repos, org or personal.

     Create a new GitHub repository via the gh CLI. Supports private/public repos, org or personal.

    Args:
        body (CreateGithubRepoRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CreateGithubRepoResponse | CreateGithubRepoResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
