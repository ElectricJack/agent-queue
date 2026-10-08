from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.session_cleanup_request import SessionCleanupRequest
from ...models.session_cleanup_response_422 import SessionCleanupResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: SessionCleanupRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/session-cleanup",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | SessionCleanupResponse422 | None:
    if response.status_code == 200:
        response_200 = response.json()
        return response_200

    if response.status_code == 422:
        response_422 = SessionCleanupResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Any | SessionCleanupResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SessionCleanupRequest,
) -> Response[Any | SessionCleanupResponse422]:
    """Stop and prune taskless sleeping named sessions, including those with live terminals. Dry-run lists
    eligible sessions without changing them.

     Stop and prune taskless sleeping named sessions, including those with live terminals. Dry-run lists
    eligible sessions without changing them.

    Args:
        body (SessionCleanupRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | SessionCleanupResponse422]
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
    body: SessionCleanupRequest,
) -> Any | SessionCleanupResponse422 | None:
    """Stop and prune taskless sleeping named sessions, including those with live terminals. Dry-run lists
    eligible sessions without changing them.

     Stop and prune taskless sleeping named sessions, including those with live terminals. Dry-run lists
    eligible sessions without changing them.

    Args:
        body (SessionCleanupRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | SessionCleanupResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SessionCleanupRequest,
) -> Response[Any | SessionCleanupResponse422]:
    """Stop and prune taskless sleeping named sessions, including those with live terminals. Dry-run lists
    eligible sessions without changing them.

     Stop and prune taskless sleeping named sessions, including those with live terminals. Dry-run lists
    eligible sessions without changing them.

    Args:
        body (SessionCleanupRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | SessionCleanupResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: SessionCleanupRequest,
) -> Any | SessionCleanupResponse422 | None:
    """Stop and prune taskless sleeping named sessions, including those with live terminals. Dry-run lists
    eligible sessions without changing them.

     Stop and prune taskless sleeping named sessions, including those with live terminals. Dry-run lists
    eligible sessions without changing them.

    Args:
        body (SessionCleanupRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | SessionCleanupResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
