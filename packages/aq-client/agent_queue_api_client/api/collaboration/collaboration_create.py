from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.collaboration_create_args import CollaborationCreateArgs
from ...models.collaboration_create_response_422 import CollaborationCreateResponse422
from ...models.collaboration_response import CollaborationResponse
from ...types import Response


def _get_kwargs(
    *,
    body: CollaborationCreateArgs,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/collaboration/create",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> CollaborationCreateResponse422 | CollaborationResponse | None:
    if response.status_code == 200:
        response_200 = CollaborationResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = CollaborationCreateResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[CollaborationCreateResponse422 | CollaborationResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CollaborationCreateArgs,
) -> Response[CollaborationCreateResponse422 | CollaborationResponse]:
    """Create a bounded collaboration thread between 2 to 4 tasks of one project and invite each once.
    Operator or supervisor only; replays on idempotency_key.

     Create a bounded collaboration thread between 2 to 4 tasks of one project and invite each once.
    Operator or supervisor only; replays on idempotency_key.

    Args:
        body (CollaborationCreateArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CollaborationCreateResponse422 | CollaborationResponse]
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
    body: CollaborationCreateArgs,
) -> CollaborationCreateResponse422 | CollaborationResponse | None:
    """Create a bounded collaboration thread between 2 to 4 tasks of one project and invite each once.
    Operator or supervisor only; replays on idempotency_key.

     Create a bounded collaboration thread between 2 to 4 tasks of one project and invite each once.
    Operator or supervisor only; replays on idempotency_key.

    Args:
        body (CollaborationCreateArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CollaborationCreateResponse422 | CollaborationResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CollaborationCreateArgs,
) -> Response[CollaborationCreateResponse422 | CollaborationResponse]:
    """Create a bounded collaboration thread between 2 to 4 tasks of one project and invite each once.
    Operator or supervisor only; replays on idempotency_key.

     Create a bounded collaboration thread between 2 to 4 tasks of one project and invite each once.
    Operator or supervisor only; replays on idempotency_key.

    Args:
        body (CollaborationCreateArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CollaborationCreateResponse422 | CollaborationResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: CollaborationCreateArgs,
) -> CollaborationCreateResponse422 | CollaborationResponse | None:
    """Create a bounded collaboration thread between 2 to 4 tasks of one project and invite each once.
    Operator or supervisor only; replays on idempotency_key.

     Create a bounded collaboration thread between 2 to 4 tasks of one project and invite each once.
    Operator or supervisor only; replays on idempotency_key.

    Args:
        body (CollaborationCreateArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CollaborationCreateResponse422 | CollaborationResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
