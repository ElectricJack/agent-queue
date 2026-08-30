from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.update_config_request import UpdateConfigRequest
from ...models.update_config_response import UpdateConfigResponse
from ...models.update_config_response_422 import UpdateConfigResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: UpdateConfigRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/update-config",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> UpdateConfigResponse | UpdateConfigResponse422 | None:
    if response.status_code == 200:
        response_200 = UpdateConfigResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = UpdateConfigResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[UpdateConfigResponse | UpdateConfigResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: UpdateConfigRequest,
) -> Response[UpdateConfigResponse | UpdateConfigResponse422]:
    """Replace one top-level section in the YAML config and trigger a hot reload for hot-reloadable
    sections. Validates the candidate doc by running load_config() against a temp file before writing,
    so a bad edit never lands on disk. Pass `data: null` to delete the section. Pass `dry_run: true` to
    validate without writing.

     Replace one top-level section in the YAML config and trigger a hot reload for hot-reloadable
    sections. Validates the candidate doc by running load_config() against a temp file before writing,
    so a bad edit never lands on disk. Pass `data: null` to delete the section. Pass `dry_run: true` to
    validate without writing.

    Args:
        body (UpdateConfigRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[UpdateConfigResponse | UpdateConfigResponse422]
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
    body: UpdateConfigRequest,
) -> UpdateConfigResponse | UpdateConfigResponse422 | None:
    """Replace one top-level section in the YAML config and trigger a hot reload for hot-reloadable
    sections. Validates the candidate doc by running load_config() against a temp file before writing,
    so a bad edit never lands on disk. Pass `data: null` to delete the section. Pass `dry_run: true` to
    validate without writing.

     Replace one top-level section in the YAML config and trigger a hot reload for hot-reloadable
    sections. Validates the candidate doc by running load_config() against a temp file before writing,
    so a bad edit never lands on disk. Pass `data: null` to delete the section. Pass `dry_run: true` to
    validate without writing.

    Args:
        body (UpdateConfigRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        UpdateConfigResponse | UpdateConfigResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: UpdateConfigRequest,
) -> Response[UpdateConfigResponse | UpdateConfigResponse422]:
    """Replace one top-level section in the YAML config and trigger a hot reload for hot-reloadable
    sections. Validates the candidate doc by running load_config() against a temp file before writing,
    so a bad edit never lands on disk. Pass `data: null` to delete the section. Pass `dry_run: true` to
    validate without writing.

     Replace one top-level section in the YAML config and trigger a hot reload for hot-reloadable
    sections. Validates the candidate doc by running load_config() against a temp file before writing,
    so a bad edit never lands on disk. Pass `data: null` to delete the section. Pass `dry_run: true` to
    validate without writing.

    Args:
        body (UpdateConfigRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[UpdateConfigResponse | UpdateConfigResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: UpdateConfigRequest,
) -> UpdateConfigResponse | UpdateConfigResponse422 | None:
    """Replace one top-level section in the YAML config and trigger a hot reload for hot-reloadable
    sections. Validates the candidate doc by running load_config() against a temp file before writing,
    so a bad edit never lands on disk. Pass `data: null` to delete the section. Pass `dry_run: true` to
    validate without writing.

     Replace one top-level section in the YAML config and trigger a hot reload for hot-reloadable
    sections. Validates the candidate doc by running load_config() against a temp file before writing,
    so a bad edit never lands on disk. Pass `data: null` to delete the section. Pass `dry_run: true` to
    validate without writing.

    Args:
        body (UpdateConfigRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        UpdateConfigResponse | UpdateConfigResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
