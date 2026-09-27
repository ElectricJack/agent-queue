from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.collaboration_accept_args import CollaborationAcceptArgs
from ...models.collaboration_accept_response_422 import CollaborationAcceptResponse422
from ...models.collaboration_response import CollaborationResponse
from ...types import Response


def _get_kwargs(
    *,
    body: CollaborationAcceptArgs,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/collaboration/accept",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> CollaborationAcceptResponse422 | CollaborationResponse | None:
    if response.status_code == 200:
        response_200 = CollaborationResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = CollaborationAcceptResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[CollaborationAcceptResponse422 | CollaborationResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CollaborationAcceptArgs,
) -> Response[CollaborationAcceptResponse422 | CollaborationResponse]:
    """Join a collaboration thread for the held task's live claim.

     Join a collaboration thread for the held task's live claim.

    Args:
        body (CollaborationAcceptArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CollaborationAcceptResponse422 | CollaborationResponse]
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
    body: CollaborationAcceptArgs,
) -> CollaborationAcceptResponse422 | CollaborationResponse | None:
    """Join a collaboration thread for the held task's live claim.

     Join a collaboration thread for the held task's live claim.

    Args:
        body (CollaborationAcceptArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CollaborationAcceptResponse422 | CollaborationResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CollaborationAcceptArgs,
) -> Response[CollaborationAcceptResponse422 | CollaborationResponse]:
    """Join a collaboration thread for the held task's live claim.

     Join a collaboration thread for the held task's live claim.

    Args:
        body (CollaborationAcceptArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CollaborationAcceptResponse422 | CollaborationResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: CollaborationAcceptArgs,
) -> CollaborationAcceptResponse422 | CollaborationResponse | None:
    """Join a collaboration thread for the held task's live claim.

     Join a collaboration thread for the held task's live claim.

    Args:
        body (CollaborationAcceptArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CollaborationAcceptResponse422 | CollaborationResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
