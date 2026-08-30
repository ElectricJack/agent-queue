from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.get_playbook_source_request import GetPlaybookSourceRequest
from ...models.get_playbook_source_response import GetPlaybookSourceResponse
from ...models.get_playbook_source_response_422 import GetPlaybookSourceResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: GetPlaybookSourceRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/get-source",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> GetPlaybookSourceResponse | GetPlaybookSourceResponse422 | None:
    if response.status_code == 200:
        response_200 = GetPlaybookSourceResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = GetPlaybookSourceResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[GetPlaybookSourceResponse | GetPlaybookSourceResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetPlaybookSourceRequest,
) -> Response[GetPlaybookSourceResponse | GetPlaybookSourceResponse422]:
    """Return the raw markdown of a playbook plus its content hash. Used by the dashboard to load a
    playbook for editing; the hash is sent back on save for optimistic-concurrency conflict detection.

     Return the raw markdown of a playbook plus its content hash. Used by the dashboard to load a
    playbook for editing; the hash is sent back on save for optimistic-concurrency conflict detection.

    Args:
        body (GetPlaybookSourceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetPlaybookSourceResponse | GetPlaybookSourceResponse422]
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
    body: GetPlaybookSourceRequest,
) -> GetPlaybookSourceResponse | GetPlaybookSourceResponse422 | None:
    """Return the raw markdown of a playbook plus its content hash. Used by the dashboard to load a
    playbook for editing; the hash is sent back on save for optimistic-concurrency conflict detection.

     Return the raw markdown of a playbook plus its content hash. Used by the dashboard to load a
    playbook for editing; the hash is sent back on save for optimistic-concurrency conflict detection.

    Args:
        body (GetPlaybookSourceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetPlaybookSourceResponse | GetPlaybookSourceResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetPlaybookSourceRequest,
) -> Response[GetPlaybookSourceResponse | GetPlaybookSourceResponse422]:
    """Return the raw markdown of a playbook plus its content hash. Used by the dashboard to load a
    playbook for editing; the hash is sent back on save for optimistic-concurrency conflict detection.

     Return the raw markdown of a playbook plus its content hash. Used by the dashboard to load a
    playbook for editing; the hash is sent back on save for optimistic-concurrency conflict detection.

    Args:
        body (GetPlaybookSourceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetPlaybookSourceResponse | GetPlaybookSourceResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: GetPlaybookSourceRequest,
) -> GetPlaybookSourceResponse | GetPlaybookSourceResponse422 | None:
    """Return the raw markdown of a playbook plus its content hash. Used by the dashboard to load a
    playbook for editing; the hash is sent back on save for optimistic-concurrency conflict detection.

     Return the raw markdown of a playbook plus its content hash. Used by the dashboard to load a
    playbook for editing; the hash is sent back on save for optimistic-concurrency conflict detection.

    Args:
        body (GetPlaybookSourceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetPlaybookSourceResponse | GetPlaybookSourceResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
