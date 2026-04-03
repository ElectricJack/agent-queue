from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.read_note_request import ReadNoteRequest
from ...models.read_note_response import ReadNoteResponse
from ...models.read_note_response_422 import ReadNoteResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ReadNoteRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/memory/read-note",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ReadNoteResponse | ReadNoteResponse422 | None:
    if response.status_code == 200:
        response_200 = ReadNoteResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ReadNoteResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ReadNoteResponse | ReadNoteResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ReadNoteRequest,
) -> Response[ReadNoteResponse | ReadNoteResponse422]:
    """Read a note's full contents. Returns the markdown content, path, and size. Use the 'name' field from
    list_notes (e.g. 'my-note.md') as the title parameter.

     Read a note's full contents. Returns the markdown content, path, and size. Use the 'name' field from
    list_notes (e.g. 'my-note.md') as the title parameter.

    Args:
        body (ReadNoteRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ReadNoteResponse | ReadNoteResponse422]
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
    body: ReadNoteRequest,
) -> ReadNoteResponse | ReadNoteResponse422 | None:
    """Read a note's full contents. Returns the markdown content, path, and size. Use the 'name' field from
    list_notes (e.g. 'my-note.md') as the title parameter.

     Read a note's full contents. Returns the markdown content, path, and size. Use the 'name' field from
    list_notes (e.g. 'my-note.md') as the title parameter.

    Args:
        body (ReadNoteRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ReadNoteResponse | ReadNoteResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ReadNoteRequest,
) -> Response[ReadNoteResponse | ReadNoteResponse422]:
    """Read a note's full contents. Returns the markdown content, path, and size. Use the 'name' field from
    list_notes (e.g. 'my-note.md') as the title parameter.

     Read a note's full contents. Returns the markdown content, path, and size. Use the 'name' field from
    list_notes (e.g. 'my-note.md') as the title parameter.

    Args:
        body (ReadNoteRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ReadNoteResponse | ReadNoteResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ReadNoteRequest,
) -> ReadNoteResponse | ReadNoteResponse422 | None:
    """Read a note's full contents. Returns the markdown content, path, and size. Use the 'name' field from
    list_notes (e.g. 'my-note.md') as the title parameter.

     Read a note's full contents. Returns the markdown content, path, and size. Use the 'name' field from
    list_notes (e.g. 'my-note.md') as the title parameter.

    Args:
        body (ReadNoteRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ReadNoteResponse | ReadNoteResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
