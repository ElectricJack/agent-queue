from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.git_hub_issue_rejection_response import GitHubIssueRejectionResponse
from ...models.github_issue_rejection_request import GithubIssueRejectionRequest
from ...models.github_issue_rejection_response_422 import GithubIssueRejectionResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: GithubIssueRejectionRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/github_issue/rejection",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> GitHubIssueRejectionResponse | GithubIssueRejectionResponse422 | None:
    if response.status_code == 200:
        response_200 = GitHubIssueRejectionResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = GithubIssueRejectionResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[GitHubIssueRejectionResponse | GithubIssueRejectionResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GithubIssueRejectionRequest,
) -> Response[GitHubIssueRejectionResponse | GithubIssueRejectionResponse422]:
    """Apply only an explicit closure request in Jack's rejected issue review.

     Apply only an explicit closure request in Jack's rejected issue review.

    Args:
        body (GithubIssueRejectionRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GitHubIssueRejectionResponse | GithubIssueRejectionResponse422]
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
    body: GithubIssueRejectionRequest,
) -> GitHubIssueRejectionResponse | GithubIssueRejectionResponse422 | None:
    """Apply only an explicit closure request in Jack's rejected issue review.

     Apply only an explicit closure request in Jack's rejected issue review.

    Args:
        body (GithubIssueRejectionRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GitHubIssueRejectionResponse | GithubIssueRejectionResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GithubIssueRejectionRequest,
) -> Response[GitHubIssueRejectionResponse | GithubIssueRejectionResponse422]:
    """Apply only an explicit closure request in Jack's rejected issue review.

     Apply only an explicit closure request in Jack's rejected issue review.

    Args:
        body (GithubIssueRejectionRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GitHubIssueRejectionResponse | GithubIssueRejectionResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: GithubIssueRejectionRequest,
) -> GitHubIssueRejectionResponse | GithubIssueRejectionResponse422 | None:
    """Apply only an explicit closure request in Jack's rejected issue review.

     Apply only an explicit closure request in Jack's rejected issue review.

    Args:
        body (GithubIssueRejectionRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GitHubIssueRejectionResponse | GithubIssueRejectionResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
