from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.knowledge_diff_request import KnowledgeDiffRequest
from ...models.knowledge_diff_response import KnowledgeDiffResponse
from ...models.knowledge_diff_response_422 import KnowledgeDiffResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: KnowledgeDiffRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/knowledge/diff",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> KnowledgeDiffResponse | KnowledgeDiffResponse422 | None:
    if response.status_code == 200:
        response_200 = KnowledgeDiffResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = KnowledgeDiffResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[KnowledgeDiffResponse | KnowledgeDiffResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: KnowledgeDiffRequest,
) -> Response[KnowledgeDiffResponse | KnowledgeDiffResponse422]:
    """Diff two exact, readable revisions of a knowledge record.

     Diff two exact, readable revisions of a knowledge record.

    Args:
        body (KnowledgeDiffRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[KnowledgeDiffResponse | KnowledgeDiffResponse422]
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
    body: KnowledgeDiffRequest,
) -> KnowledgeDiffResponse | KnowledgeDiffResponse422 | None:
    """Diff two exact, readable revisions of a knowledge record.

     Diff two exact, readable revisions of a knowledge record.

    Args:
        body (KnowledgeDiffRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        KnowledgeDiffResponse | KnowledgeDiffResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: KnowledgeDiffRequest,
) -> Response[KnowledgeDiffResponse | KnowledgeDiffResponse422]:
    """Diff two exact, readable revisions of a knowledge record.

     Diff two exact, readable revisions of a knowledge record.

    Args:
        body (KnowledgeDiffRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[KnowledgeDiffResponse | KnowledgeDiffResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: KnowledgeDiffRequest,
) -> KnowledgeDiffResponse | KnowledgeDiffResponse422 | None:
    """Diff two exact, readable revisions of a knowledge record.

     Diff two exact, readable revisions of a knowledge record.

    Args:
        body (KnowledgeDiffRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        KnowledgeDiffResponse | KnowledgeDiffResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
