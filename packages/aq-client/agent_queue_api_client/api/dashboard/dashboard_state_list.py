from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.dashboard_state_error_response import DashboardStateErrorResponse
from ...models.dashboard_state_list_request import DashboardStateListRequest
from ...models.dashboard_state_list_response import DashboardStateListResponse
from ...types import Response


def _get_kwargs(
    *,
    body: DashboardStateListRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/dashboard/state-list",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> DashboardStateErrorResponse | DashboardStateListResponse | None:
    if response.status_code == 200:
        response_200 = DashboardStateListResponse.from_dict(response.json())

        return response_200

    if response.status_code == 403:
        response_403 = DashboardStateErrorResponse.from_dict(response.json())

        return response_403

    if response.status_code == 422:
        response_422 = DashboardStateErrorResponse.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[DashboardStateErrorResponse | DashboardStateListResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DashboardStateListRequest,
) -> Response[DashboardStateErrorResponse | DashboardStateListResponse]:
    """List every shared and caller-owned dashboard state document.

     List every shared and caller-owned dashboard state document.

    Args:
        body (DashboardStateListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DashboardStateErrorResponse | DashboardStateListResponse]
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
    body: DashboardStateListRequest,
) -> DashboardStateErrorResponse | DashboardStateListResponse | None:
    """List every shared and caller-owned dashboard state document.

     List every shared and caller-owned dashboard state document.

    Args:
        body (DashboardStateListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DashboardStateErrorResponse | DashboardStateListResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DashboardStateListRequest,
) -> Response[DashboardStateErrorResponse | DashboardStateListResponse]:
    """List every shared and caller-owned dashboard state document.

     List every shared and caller-owned dashboard state document.

    Args:
        body (DashboardStateListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DashboardStateErrorResponse | DashboardStateListResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: DashboardStateListRequest,
) -> DashboardStateErrorResponse | DashboardStateListResponse | None:
    """List every shared and caller-owned dashboard state document.

     List every shared and caller-owned dashboard state document.

    Args:
        body (DashboardStateListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DashboardStateErrorResponse | DashboardStateListResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
