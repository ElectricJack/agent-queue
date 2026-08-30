from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.scan_stub_staleness_request import ScanStubStalenessRequest
from ...models.scan_stub_staleness_response import ScanStubStalenessResponse
from ...models.scan_stub_staleness_response_422 import ScanStubStalenessResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ScanStubStalenessRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/scan-stub-staleness",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ScanStubStalenessResponse | ScanStubStalenessResponse422 | None:
    if response.status_code == 200:
        response_200 = ScanStubStalenessResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ScanStubStalenessResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ScanStubStalenessResponse | ScanStubStalenessResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ScanStubStalenessRequest,
) -> Response[ScanStubStalenessResponse | ScanStubStalenessResponse422]:
    """Scan vault reference stubs to detect staleness.  Compares each stub's recorded source_hash against
    the current source file on disk.  Reports stubs that are stale (source changed), missing (source
    deleted), unenriched (placeholder content), or orphaned (no source metadata).  Use to audit
    reference stub health before triggering re-enrichment.

     Scan vault reference stubs to detect staleness.  Compares each stub's recorded source_hash against
    the current source file on disk.  Reports stubs that are stale (source changed), missing (source
    deleted), unenriched (placeholder content), or orphaned (no source metadata).  Use to audit
    reference stub health before triggering re-enrichment.

    Args:
        body (ScanStubStalenessRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ScanStubStalenessResponse | ScanStubStalenessResponse422]
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
    body: ScanStubStalenessRequest,
) -> ScanStubStalenessResponse | ScanStubStalenessResponse422 | None:
    """Scan vault reference stubs to detect staleness.  Compares each stub's recorded source_hash against
    the current source file on disk.  Reports stubs that are stale (source changed), missing (source
    deleted), unenriched (placeholder content), or orphaned (no source metadata).  Use to audit
    reference stub health before triggering re-enrichment.

     Scan vault reference stubs to detect staleness.  Compares each stub's recorded source_hash against
    the current source file on disk.  Reports stubs that are stale (source changed), missing (source
    deleted), unenriched (placeholder content), or orphaned (no source metadata).  Use to audit
    reference stub health before triggering re-enrichment.

    Args:
        body (ScanStubStalenessRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ScanStubStalenessResponse | ScanStubStalenessResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ScanStubStalenessRequest,
) -> Response[ScanStubStalenessResponse | ScanStubStalenessResponse422]:
    """Scan vault reference stubs to detect staleness.  Compares each stub's recorded source_hash against
    the current source file on disk.  Reports stubs that are stale (source changed), missing (source
    deleted), unenriched (placeholder content), or orphaned (no source metadata).  Use to audit
    reference stub health before triggering re-enrichment.

     Scan vault reference stubs to detect staleness.  Compares each stub's recorded source_hash against
    the current source file on disk.  Reports stubs that are stale (source changed), missing (source
    deleted), unenriched (placeholder content), or orphaned (no source metadata).  Use to audit
    reference stub health before triggering re-enrichment.

    Args:
        body (ScanStubStalenessRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ScanStubStalenessResponse | ScanStubStalenessResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ScanStubStalenessRequest,
) -> ScanStubStalenessResponse | ScanStubStalenessResponse422 | None:
    """Scan vault reference stubs to detect staleness.  Compares each stub's recorded source_hash against
    the current source file on disk.  Reports stubs that are stale (source changed), missing (source
    deleted), unenriched (placeholder content), or orphaned (no source metadata).  Use to audit
    reference stub health before triggering re-enrichment.

     Scan vault reference stubs to detect staleness.  Compares each stub's recorded source_hash against
    the current source file on disk.  Reports stubs that are stale (source changed), missing (source
    deleted), unenriched (placeholder content), or orphaned (no source metadata).  Use to audit
    reference stub health before triggering re-enrichment.

    Args:
        body (ScanStubStalenessRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ScanStubStalenessResponse | ScanStubStalenessResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
