from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.dashboard_state_conflict_response import DashboardStateConflictResponse
from ...models.dashboard_state_document_response import DashboardStateDocumentResponse
from ...models.dashboard_state_error_response import DashboardStateErrorResponse
from ...models.dashboard_state_put_request import DashboardStatePutRequest
from ...types import Response


def _get_kwargs(
    *,
    body: DashboardStatePutRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/dashboard/state-put",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> DashboardStateConflictResponse | DashboardStateDocumentResponse | DashboardStateErrorResponse | None:
    if response.status_code == 200:
        response_200 = DashboardStateDocumentResponse.from_dict(response.json())

        return response_200

    if response.status_code == 403:
        response_403 = DashboardStateErrorResponse.from_dict(response.json())

        return response_403

    if response.status_code == 409:
        response_409 = DashboardStateConflictResponse.from_dict(response.json())

        return response_409

    if response.status_code == 422:
        response_422 = DashboardStateErrorResponse.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[DashboardStateConflictResponse | DashboardStateDocumentResponse | DashboardStateErrorResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DashboardStatePutRequest,
) -> Response[DashboardStateConflictResponse | DashboardStateDocumentResponse | DashboardStateErrorResponse]:
    """Replace one validated dashboard state document.

     Replace one validated dashboard state document.

    Args:
        body (DashboardStatePutRequest): Flat fallback keeps command-level validation/error
            envelopes intact.

            Response documents still carry the discriminated value union, so both
            generated clients expose every namespace value type without FastAPI
            pre-empting ``invalid_value`` and ``unknown_namespace`` with its generic
            request-validation response.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DashboardStateConflictResponse | DashboardStateDocumentResponse | DashboardStateErrorResponse]
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
    body: DashboardStatePutRequest,
) -> DashboardStateConflictResponse | DashboardStateDocumentResponse | DashboardStateErrorResponse | None:
    """Replace one validated dashboard state document.

     Replace one validated dashboard state document.

    Args:
        body (DashboardStatePutRequest): Flat fallback keeps command-level validation/error
            envelopes intact.

            Response documents still carry the discriminated value union, so both
            generated clients expose every namespace value type without FastAPI
            pre-empting ``invalid_value`` and ``unknown_namespace`` with its generic
            request-validation response.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DashboardStateConflictResponse | DashboardStateDocumentResponse | DashboardStateErrorResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DashboardStatePutRequest,
) -> Response[DashboardStateConflictResponse | DashboardStateDocumentResponse | DashboardStateErrorResponse]:
    """Replace one validated dashboard state document.

     Replace one validated dashboard state document.

    Args:
        body (DashboardStatePutRequest): Flat fallback keeps command-level validation/error
            envelopes intact.

            Response documents still carry the discriminated value union, so both
            generated clients expose every namespace value type without FastAPI
            pre-empting ``invalid_value`` and ``unknown_namespace`` with its generic
            request-validation response.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DashboardStateConflictResponse | DashboardStateDocumentResponse | DashboardStateErrorResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: DashboardStatePutRequest,
) -> DashboardStateConflictResponse | DashboardStateDocumentResponse | DashboardStateErrorResponse | None:
    """Replace one validated dashboard state document.

     Replace one validated dashboard state document.

    Args:
        body (DashboardStatePutRequest): Flat fallback keeps command-level validation/error
            envelopes intact.

            Response documents still carry the discriminated value union, so both
            generated clients expose every namespace value type without FastAPI
            pre-empting ``invalid_value`` and ``unknown_namespace`` with its generic
            request-validation response.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DashboardStateConflictResponse | DashboardStateDocumentResponse | DashboardStateErrorResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
