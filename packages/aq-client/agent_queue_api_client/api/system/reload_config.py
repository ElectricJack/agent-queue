from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.reload_config_request import ReloadConfigRequest
from ...models.reload_config_response import ReloadConfigResponse
from ...models.reload_config_response_422 import ReloadConfigResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ReloadConfigRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/reload-config",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ReloadConfigResponse | ReloadConfigResponse422 | None:
    if response.status_code == 200:
        response_200 = ReloadConfigResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ReloadConfigResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ReloadConfigResponse | ReloadConfigResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ReloadConfigRequest,
) -> Response[ReloadConfigResponse | ReloadConfigResponse422]:
    """Manually trigger a config hot-reload from disk. Returns a summary of which sections changed, which
    were applied, and which require a daemon restart.

     Manually trigger a config hot-reload from disk. Returns a summary of which sections changed, which
    were applied, and which require a daemon restart.

    Args:
        body (ReloadConfigRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ReloadConfigResponse | ReloadConfigResponse422]
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
    body: ReloadConfigRequest,
) -> ReloadConfigResponse | ReloadConfigResponse422 | None:
    """Manually trigger a config hot-reload from disk. Returns a summary of which sections changed, which
    were applied, and which require a daemon restart.

     Manually trigger a config hot-reload from disk. Returns a summary of which sections changed, which
    were applied, and which require a daemon restart.

    Args:
        body (ReloadConfigRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ReloadConfigResponse | ReloadConfigResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ReloadConfigRequest,
) -> Response[ReloadConfigResponse | ReloadConfigResponse422]:
    """Manually trigger a config hot-reload from disk. Returns a summary of which sections changed, which
    were applied, and which require a daemon restart.

     Manually trigger a config hot-reload from disk. Returns a summary of which sections changed, which
    were applied, and which require a daemon restart.

    Args:
        body (ReloadConfigRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ReloadConfigResponse | ReloadConfigResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ReloadConfigRequest,
) -> ReloadConfigResponse | ReloadConfigResponse422 | None:
    """Manually trigger a config hot-reload from disk. Returns a summary of which sections changed, which
    were applied, and which require a daemon restart.

     Manually trigger a config hot-reload from disk. Returns a summary of which sections changed, which
    were applied, and which require a daemon restart.

    Args:
        body (ReloadConfigRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ReloadConfigResponse | ReloadConfigResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
