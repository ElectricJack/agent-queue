from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.approve_pull_request_request import ApprovePullRequestRequest
from ...models.approve_pull_request_response_422 import ApprovePullRequestResponse422
from ...models.pull_request_approve_response import PullRequestApproveResponse
from ...types import Response


def _get_kwargs(
    *,
    body: ApprovePullRequestRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/review/approve-pull-request",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ApprovePullRequestResponse422 | PullRequestApproveResponse | None:
    if response.status_code == 200:
        response_200 = PullRequestApproveResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ApprovePullRequestResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ApprovePullRequestResponse422 | PullRequestApproveResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ApprovePullRequestRequest,
) -> Response[ApprovePullRequestResponse422 | PullRequestApproveResponse]:
    """Approve a task-linked GitHub PR at its displayed head as the local operator's saved gh user.

     Approve a task-linked GitHub PR at its displayed head as the local operator's saved gh user.

    Args:
        body (ApprovePullRequestRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApprovePullRequestResponse422 | PullRequestApproveResponse]
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
    body: ApprovePullRequestRequest,
) -> ApprovePullRequestResponse422 | PullRequestApproveResponse | None:
    """Approve a task-linked GitHub PR at its displayed head as the local operator's saved gh user.

     Approve a task-linked GitHub PR at its displayed head as the local operator's saved gh user.

    Args:
        body (ApprovePullRequestRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApprovePullRequestResponse422 | PullRequestApproveResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ApprovePullRequestRequest,
) -> Response[ApprovePullRequestResponse422 | PullRequestApproveResponse]:
    """Approve a task-linked GitHub PR at its displayed head as the local operator's saved gh user.

     Approve a task-linked GitHub PR at its displayed head as the local operator's saved gh user.

    Args:
        body (ApprovePullRequestRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApprovePullRequestResponse422 | PullRequestApproveResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ApprovePullRequestRequest,
) -> ApprovePullRequestResponse422 | PullRequestApproveResponse | None:
    """Approve a task-linked GitHub PR at its displayed head as the local operator's saved gh user.

     Approve a task-linked GitHub PR at its displayed head as the local operator's saved gh user.

    Args:
        body (ApprovePullRequestRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApprovePullRequestResponse422 | PullRequestApproveResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
