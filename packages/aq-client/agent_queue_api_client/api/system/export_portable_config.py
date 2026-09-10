from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.export_portable_config_request import ExportPortableConfigRequest
from ...models.export_portable_config_response_422 import ExportPortableConfigResponse422
from ...models.portable_config_response import PortableConfigResponse
from ...types import Response


def _get_kwargs(
    *,
    body: ExportPortableConfigRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/export-portable-config",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ExportPortableConfigResponse422 | PortableConfigResponse | None:
    if response.status_code == 200:
        response_200 = PortableConfigResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ExportPortableConfigResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ExportPortableConfigResponse422 | PortableConfigResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ExportPortableConfigRequest,
) -> Response[ExportPortableConfigResponse422 | PortableConfigResponse]:
    """Write a versioned, validated .aqbundle containing only portable tuning and global profile
    definitions. Run preview_portable_config first to curate the exported defaults.

     Write a versioned, validated .aqbundle containing only portable tuning and global profile
    definitions. Run preview_portable_config first to curate the exported defaults.

    Args:
        body (ExportPortableConfigRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ExportPortableConfigResponse422 | PortableConfigResponse]
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
    body: ExportPortableConfigRequest,
) -> ExportPortableConfigResponse422 | PortableConfigResponse | None:
    """Write a versioned, validated .aqbundle containing only portable tuning and global profile
    definitions. Run preview_portable_config first to curate the exported defaults.

     Write a versioned, validated .aqbundle containing only portable tuning and global profile
    definitions. Run preview_portable_config first to curate the exported defaults.

    Args:
        body (ExportPortableConfigRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ExportPortableConfigResponse422 | PortableConfigResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ExportPortableConfigRequest,
) -> Response[ExportPortableConfigResponse422 | PortableConfigResponse]:
    """Write a versioned, validated .aqbundle containing only portable tuning and global profile
    definitions. Run preview_portable_config first to curate the exported defaults.

     Write a versioned, validated .aqbundle containing only portable tuning and global profile
    definitions. Run preview_portable_config first to curate the exported defaults.

    Args:
        body (ExportPortableConfigRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ExportPortableConfigResponse422 | PortableConfigResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ExportPortableConfigRequest,
) -> ExportPortableConfigResponse422 | PortableConfigResponse | None:
    """Write a versioned, validated .aqbundle containing only portable tuning and global profile
    definitions. Run preview_portable_config first to curate the exported defaults.

     Write a versioned, validated .aqbundle containing only portable tuning and global profile
    definitions. Run preview_portable_config first to curate the exported defaults.

    Args:
        body (ExportPortableConfigRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ExportPortableConfigResponse422 | PortableConfigResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
