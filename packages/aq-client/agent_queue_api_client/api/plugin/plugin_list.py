from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.plugin_list_request import PluginListRequest
from ...models.plugin_list_response import PluginListResponse
from ...models.plugin_list_response_422 import PluginListResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PluginListRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/plugin/list",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PluginListResponse | PluginListResponse422 | None:
    if response.status_code == 200:
        response_200 = PluginListResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PluginListResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PluginListResponse | PluginListResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PluginListRequest,
) -> Response[PluginListResponse | PluginListResponse422]:
    """List all installed plugins with their status.

     List all installed plugins with their status.

    Args:
        body (PluginListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PluginListResponse | PluginListResponse422]
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
    body: PluginListRequest,
) -> PluginListResponse | PluginListResponse422 | None:
    """List all installed plugins with their status.

     List all installed plugins with their status.

    Args:
        body (PluginListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PluginListResponse | PluginListResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PluginListRequest,
) -> Response[PluginListResponse | PluginListResponse422]:
    """List all installed plugins with their status.

     List all installed plugins with their status.

    Args:
        body (PluginListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PluginListResponse | PluginListResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PluginListRequest,
) -> PluginListResponse | PluginListResponse422 | None:
    """List all installed plugins with their status.

     List all installed plugins with their status.

    Args:
        body (PluginListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PluginListResponse | PluginListResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
