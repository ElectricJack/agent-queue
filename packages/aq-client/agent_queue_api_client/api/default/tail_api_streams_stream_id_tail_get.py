from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.http_validation_error import HTTPValidationError
from ...models.tail_api_streams_stream_id_tail_get_response_tail_api_streams_stream_id_tail_get import (
    TailApiStreamsStreamIdTailGetResponseTailApiStreamsStreamIdTailGet,
)
from ...types import UNSET, Response, Unset


def _get_kwargs(
    stream_id: str,
    *,
    after_seq: int | Unset = -1,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["after_seq"] = after_seq

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/streams/{stream_id}/tail".format(
            stream_id=quote(str(stream_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> HTTPValidationError | TailApiStreamsStreamIdTailGetResponseTailApiStreamsStreamIdTailGet | None:
    if response.status_code == 200:
        response_200 = TailApiStreamsStreamIdTailGetResponseTailApiStreamsStreamIdTailGet.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[HTTPValidationError | TailApiStreamsStreamIdTailGetResponseTailApiStreamsStreamIdTailGet]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    stream_id: str,
    *,
    client: AuthenticatedClient | Client,
    after_seq: int | Unset = -1,
) -> Response[HTTPValidationError | TailApiStreamsStreamIdTailGetResponseTailApiStreamsStreamIdTailGet]:
    """Tail

    Args:
        stream_id (str):
        after_seq (int | Unset):  Default: -1.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | TailApiStreamsStreamIdTailGetResponseTailApiStreamsStreamIdTailGet]
    """

    kwargs = _get_kwargs(
        stream_id=stream_id,
        after_seq=after_seq,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    stream_id: str,
    *,
    client: AuthenticatedClient | Client,
    after_seq: int | Unset = -1,
) -> HTTPValidationError | TailApiStreamsStreamIdTailGetResponseTailApiStreamsStreamIdTailGet | None:
    """Tail

    Args:
        stream_id (str):
        after_seq (int | Unset):  Default: -1.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | TailApiStreamsStreamIdTailGetResponseTailApiStreamsStreamIdTailGet
    """

    return sync_detailed(
        stream_id=stream_id,
        client=client,
        after_seq=after_seq,
    ).parsed


async def asyncio_detailed(
    stream_id: str,
    *,
    client: AuthenticatedClient | Client,
    after_seq: int | Unset = -1,
) -> Response[HTTPValidationError | TailApiStreamsStreamIdTailGetResponseTailApiStreamsStreamIdTailGet]:
    """Tail

    Args:
        stream_id (str):
        after_seq (int | Unset):  Default: -1.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | TailApiStreamsStreamIdTailGetResponseTailApiStreamsStreamIdTailGet]
    """

    kwargs = _get_kwargs(
        stream_id=stream_id,
        after_seq=after_seq,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    stream_id: str,
    *,
    client: AuthenticatedClient | Client,
    after_seq: int | Unset = -1,
) -> HTTPValidationError | TailApiStreamsStreamIdTailGetResponseTailApiStreamsStreamIdTailGet | None:
    """Tail

    Args:
        stream_id (str):
        after_seq (int | Unset):  Default: -1.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | TailApiStreamsStreamIdTailGetResponseTailApiStreamsStreamIdTailGet
    """

    return (
        await asyncio_detailed(
            stream_id=stream_id,
            client=client,
            after_seq=after_seq,
        )
    ).parsed
