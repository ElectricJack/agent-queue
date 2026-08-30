from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.get_config_request import GetConfigRequest
from ...models.get_config_response import GetConfigResponse
from ...models.get_config_response_422 import GetConfigResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: GetConfigRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/get-config",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> GetConfigResponse | GetConfigResponse422 | None:
    if response.status_code == 200:
        response_200 = GetConfigResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = GetConfigResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[GetConfigResponse | GetConfigResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetConfigRequest,
) -> Response[GetConfigResponse | GetConfigResponse422]:
    """Return the raw YAML configuration as written on disk, preserving ${ENV_VAR} placeholders. Used by
    the dashboard config editor and the `aq system config get` CLI. Pass `section` to fetch one top-
    level section only. Includes hot-reloadable vs restart-required classification and a list of every
    ${ENV_VAR} reference with whether it currently resolves.

     Return the raw YAML configuration as written on disk, preserving ${ENV_VAR} placeholders. Used by
    the dashboard config editor and the `aq system config get` CLI. Pass `section` to fetch one top-
    level section only. Includes hot-reloadable vs restart-required classification and a list of every
    ${ENV_VAR} reference with whether it currently resolves.

    Args:
        body (GetConfigRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetConfigResponse | GetConfigResponse422]
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
    body: GetConfigRequest,
) -> GetConfigResponse | GetConfigResponse422 | None:
    """Return the raw YAML configuration as written on disk, preserving ${ENV_VAR} placeholders. Used by
    the dashboard config editor and the `aq system config get` CLI. Pass `section` to fetch one top-
    level section only. Includes hot-reloadable vs restart-required classification and a list of every
    ${ENV_VAR} reference with whether it currently resolves.

     Return the raw YAML configuration as written on disk, preserving ${ENV_VAR} placeholders. Used by
    the dashboard config editor and the `aq system config get` CLI. Pass `section` to fetch one top-
    level section only. Includes hot-reloadable vs restart-required classification and a list of every
    ${ENV_VAR} reference with whether it currently resolves.

    Args:
        body (GetConfigRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetConfigResponse | GetConfigResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetConfigRequest,
) -> Response[GetConfigResponse | GetConfigResponse422]:
    """Return the raw YAML configuration as written on disk, preserving ${ENV_VAR} placeholders. Used by
    the dashboard config editor and the `aq system config get` CLI. Pass `section` to fetch one top-
    level section only. Includes hot-reloadable vs restart-required classification and a list of every
    ${ENV_VAR} reference with whether it currently resolves.

     Return the raw YAML configuration as written on disk, preserving ${ENV_VAR} placeholders. Used by
    the dashboard config editor and the `aq system config get` CLI. Pass `section` to fetch one top-
    level section only. Includes hot-reloadable vs restart-required classification and a list of every
    ${ENV_VAR} reference with whether it currently resolves.

    Args:
        body (GetConfigRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetConfigResponse | GetConfigResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: GetConfigRequest,
) -> GetConfigResponse | GetConfigResponse422 | None:
    """Return the raw YAML configuration as written on disk, preserving ${ENV_VAR} placeholders. Used by
    the dashboard config editor and the `aq system config get` CLI. Pass `section` to fetch one top-
    level section only. Includes hot-reloadable vs restart-required classification and a list of every
    ${ENV_VAR} reference with whether it currently resolves.

     Return the raw YAML configuration as written on disk, preserving ${ENV_VAR} placeholders. Used by
    the dashboard config editor and the `aq system config get` CLI. Pass `section` to fetch one top-
    level section only. Includes hot-reloadable vs restart-required classification and a list of every
    ${ENV_VAR} reference with whether it currently resolves.

    Args:
        body (GetConfigRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetConfigResponse | GetConfigResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
