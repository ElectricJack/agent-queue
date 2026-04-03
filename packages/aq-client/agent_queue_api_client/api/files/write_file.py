from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.write_file_request import WriteFileRequest
from ...models.write_file_response import WriteFileResponse
from ...models.write_file_response_422 import WriteFileResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: WriteFileRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/files/write",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> WriteFileResponse | WriteFileResponse422 | None:
    if response.status_code == 200:
        response_200 = WriteFileResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = WriteFileResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[WriteFileResponse | WriteFileResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: WriteFileRequest,
) -> Response[WriteFileResponse | WriteFileResponse422]:
    """Write content to a file. Creates the file (and parent directories) if it doesn't exist, or
    overwrites if it does. Path can be absolute or relative to the workspaces root.

     Write content to a file. Creates the file (and parent directories) if it doesn't exist, or
    overwrites if it does. Path can be absolute or relative to the workspaces root.

    Args:
        body (WriteFileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[WriteFileResponse | WriteFileResponse422]
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
    body: WriteFileRequest,
) -> WriteFileResponse | WriteFileResponse422 | None:
    """Write content to a file. Creates the file (and parent directories) if it doesn't exist, or
    overwrites if it does. Path can be absolute or relative to the workspaces root.

     Write content to a file. Creates the file (and parent directories) if it doesn't exist, or
    overwrites if it does. Path can be absolute or relative to the workspaces root.

    Args:
        body (WriteFileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        WriteFileResponse | WriteFileResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: WriteFileRequest,
) -> Response[WriteFileResponse | WriteFileResponse422]:
    """Write content to a file. Creates the file (and parent directories) if it doesn't exist, or
    overwrites if it does. Path can be absolute or relative to the workspaces root.

     Write content to a file. Creates the file (and parent directories) if it doesn't exist, or
    overwrites if it does. Path can be absolute or relative to the workspaces root.

    Args:
        body (WriteFileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[WriteFileResponse | WriteFileResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: WriteFileRequest,
) -> WriteFileResponse | WriteFileResponse422 | None:
    """Write content to a file. Creates the file (and parent directories) if it doesn't exist, or
    overwrites if it does. Path can be absolute or relative to the workspaces root.

     Write content to a file. Creates the file (and parent directories) if it doesn't exist, or
    overwrites if it does. Path can be absolute or relative to the workspaces root.

    Args:
        body (WriteFileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        WriteFileResponse | WriteFileResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
