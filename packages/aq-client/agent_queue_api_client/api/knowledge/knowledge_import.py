from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.knowledge_import_request import KnowledgeImportRequest
from ...models.knowledge_import_response import KnowledgeImportResponse
from ...models.knowledge_import_response_422 import KnowledgeImportResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: KnowledgeImportRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/knowledge/import",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> KnowledgeImportResponse | KnowledgeImportResponse422 | None:
    if response.status_code == 200:
        response_200 = KnowledgeImportResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = KnowledgeImportResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[KnowledgeImportResponse | KnowledgeImportResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: KnowledgeImportRequest,
) -> Response[KnowledgeImportResponse | KnowledgeImportResponse422]:
    """Scan, seal and verify a legacy import inventory. Read-only by default; nothing is applied or
    written.

     Scan, seal and verify a legacy import inventory. Read-only by default; nothing is applied or
    written.

    Args:
        body (KnowledgeImportRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[KnowledgeImportResponse | KnowledgeImportResponse422]
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
    body: KnowledgeImportRequest,
) -> KnowledgeImportResponse | KnowledgeImportResponse422 | None:
    """Scan, seal and verify a legacy import inventory. Read-only by default; nothing is applied or
    written.

     Scan, seal and verify a legacy import inventory. Read-only by default; nothing is applied or
    written.

    Args:
        body (KnowledgeImportRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        KnowledgeImportResponse | KnowledgeImportResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: KnowledgeImportRequest,
) -> Response[KnowledgeImportResponse | KnowledgeImportResponse422]:
    """Scan, seal and verify a legacy import inventory. Read-only by default; nothing is applied or
    written.

     Scan, seal and verify a legacy import inventory. Read-only by default; nothing is applied or
    written.

    Args:
        body (KnowledgeImportRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[KnowledgeImportResponse | KnowledgeImportResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: KnowledgeImportRequest,
) -> KnowledgeImportResponse | KnowledgeImportResponse422 | None:
    """Scan, seal and verify a legacy import inventory. Read-only by default; nothing is applied or
    written.

     Scan, seal and verify a legacy import inventory. Read-only by default; nothing is applied or
    written.

    Args:
        body (KnowledgeImportRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        KnowledgeImportResponse | KnowledgeImportResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
