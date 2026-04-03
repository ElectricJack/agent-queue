from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.glob_files_request import GlobFilesRequest
from ...models.glob_files_response import GlobFilesResponse
from ...models.glob_files_response_422 import GlobFilesResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: GlobFilesRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/files/glob",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> GlobFilesResponse | GlobFilesResponse422 | None:
    if response.status_code == 200:
        response_200 = GlobFilesResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = GlobFilesResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[GlobFilesResponse | GlobFilesResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GlobFilesRequest,
) -> Response[GlobFilesResponse | GlobFilesResponse422]:
    """Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). Returns matching file paths
    sorted by modification time.

     Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). Returns matching file paths
    sorted by modification time.

    Args:
        body (GlobFilesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GlobFilesResponse | GlobFilesResponse422]
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
    body: GlobFilesRequest,
) -> GlobFilesResponse | GlobFilesResponse422 | None:
    """Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). Returns matching file paths
    sorted by modification time.

     Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). Returns matching file paths
    sorted by modification time.

    Args:
        body (GlobFilesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GlobFilesResponse | GlobFilesResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GlobFilesRequest,
) -> Response[GlobFilesResponse | GlobFilesResponse422]:
    """Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). Returns matching file paths
    sorted by modification time.

     Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). Returns matching file paths
    sorted by modification time.

    Args:
        body (GlobFilesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GlobFilesResponse | GlobFilesResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: GlobFilesRequest,
) -> GlobFilesResponse | GlobFilesResponse422 | None:
    """Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). Returns matching file paths
    sorted by modification time.

     Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). Returns matching file paths
    sorted by modification time.

    Args:
        body (GlobFilesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GlobFilesResponse | GlobFilesResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
