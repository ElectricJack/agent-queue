from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.spec_ingest_reconcile_request import SpecIngestReconcileRequest
from ...models.spec_ingest_reconcile_response import SpecIngestReconcileResponse
from ...models.spec_ingest_reconcile_response_422 import SpecIngestReconcileResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: SpecIngestReconcileRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/spec-ingest-reconcile",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> SpecIngestReconcileResponse | SpecIngestReconcileResponse422 | None:
    if response.status_code == 200:
        response_200 = SpecIngestReconcileResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = SpecIngestReconcileResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[SpecIngestReconcileResponse | SpecIngestReconcileResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SpecIngestReconcileRequest,
) -> Response[SpecIngestReconcileResponse | SpecIngestReconcileResponse422]:
    """Replay at most twenty pending approved-spec dispatches; operator/service only.

     Replay at most twenty pending approved-spec dispatches; operator/service only.

    Args:
        body (SpecIngestReconcileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[SpecIngestReconcileResponse | SpecIngestReconcileResponse422]
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
    body: SpecIngestReconcileRequest,
) -> SpecIngestReconcileResponse | SpecIngestReconcileResponse422 | None:
    """Replay at most twenty pending approved-spec dispatches; operator/service only.

     Replay at most twenty pending approved-spec dispatches; operator/service only.

    Args:
        body (SpecIngestReconcileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        SpecIngestReconcileResponse | SpecIngestReconcileResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SpecIngestReconcileRequest,
) -> Response[SpecIngestReconcileResponse | SpecIngestReconcileResponse422]:
    """Replay at most twenty pending approved-spec dispatches; operator/service only.

     Replay at most twenty pending approved-spec dispatches; operator/service only.

    Args:
        body (SpecIngestReconcileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[SpecIngestReconcileResponse | SpecIngestReconcileResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: SpecIngestReconcileRequest,
) -> SpecIngestReconcileResponse | SpecIngestReconcileResponse422 | None:
    """Replay at most twenty pending approved-spec dispatches; operator/service only.

     Replay at most twenty pending approved-spec dispatches; operator/service only.

    Args:
        body (SpecIngestReconcileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        SpecIngestReconcileResponse | SpecIngestReconcileResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
