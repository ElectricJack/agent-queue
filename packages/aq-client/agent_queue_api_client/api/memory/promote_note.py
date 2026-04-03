from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.promote_note_request import PromoteNoteRequest
from ...models.promote_note_response import PromoteNoteResponse
from ...models.promote_note_response_422 import PromoteNoteResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PromoteNoteRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/memory/promote-note",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PromoteNoteResponse | PromoteNoteResponse422 | None:
    if response.status_code == 200:
        response_200 = PromoteNoteResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PromoteNoteResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PromoteNoteResponse | PromoteNoteResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PromoteNoteRequest,
) -> Response[PromoteNoteResponse | PromoteNoteResponse422]:
    """Explicitly incorporate a note's content into the project profile. Uses an LLM to integrate the
    note's knowledge into the living profile rather than simply appending. Use when a note contains
    important knowledge that should be part of the project's core understanding.

     Explicitly incorporate a note's content into the project profile. Uses an LLM to integrate the
    note's knowledge into the living profile rather than simply appending. Use when a note contains
    important knowledge that should be part of the project's core understanding.

    Args:
        body (PromoteNoteRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PromoteNoteResponse | PromoteNoteResponse422]
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
    body: PromoteNoteRequest,
) -> PromoteNoteResponse | PromoteNoteResponse422 | None:
    """Explicitly incorporate a note's content into the project profile. Uses an LLM to integrate the
    note's knowledge into the living profile rather than simply appending. Use when a note contains
    important knowledge that should be part of the project's core understanding.

     Explicitly incorporate a note's content into the project profile. Uses an LLM to integrate the
    note's knowledge into the living profile rather than simply appending. Use when a note contains
    important knowledge that should be part of the project's core understanding.

    Args:
        body (PromoteNoteRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PromoteNoteResponse | PromoteNoteResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PromoteNoteRequest,
) -> Response[PromoteNoteResponse | PromoteNoteResponse422]:
    """Explicitly incorporate a note's content into the project profile. Uses an LLM to integrate the
    note's knowledge into the living profile rather than simply appending. Use when a note contains
    important knowledge that should be part of the project's core understanding.

     Explicitly incorporate a note's content into the project profile. Uses an LLM to integrate the
    note's knowledge into the living profile rather than simply appending. Use when a note contains
    important knowledge that should be part of the project's core understanding.

    Args:
        body (PromoteNoteRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PromoteNoteResponse | PromoteNoteResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PromoteNoteRequest,
) -> PromoteNoteResponse | PromoteNoteResponse422 | None:
    """Explicitly incorporate a note's content into the project profile. Uses an LLM to integrate the
    note's knowledge into the living profile rather than simply appending. Use when a note contains
    important knowledge that should be part of the project's core understanding.

     Explicitly incorporate a note's content into the project profile. Uses an LLM to integrate the
    note's knowledge into the living profile rather than simply appending. Use when a note contains
    important knowledge that should be part of the project's core understanding.

    Args:
        body (PromoteNoteRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PromoteNoteResponse | PromoteNoteResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
