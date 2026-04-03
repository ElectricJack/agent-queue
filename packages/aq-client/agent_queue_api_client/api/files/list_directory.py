from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.list_directory_request import ListDirectoryRequest
from ...models.list_directory_response import ListDirectoryResponse
from ...models.list_directory_response_422 import ListDirectoryResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ListDirectoryRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/files/list-directory",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ListDirectoryResponse | ListDirectoryResponse422 | None:
    if response.status_code == 200:
        response_200 = ListDirectoryResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ListDirectoryResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ListDirectoryResponse | ListDirectoryResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListDirectoryRequest,
) -> Response[ListDirectoryResponse | ListDirectoryResponse422]:
    """List files and directories at a given path within a project workspace.

     List files and directories at a given path within a project workspace.

    Args:
        body (ListDirectoryRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListDirectoryResponse | ListDirectoryResponse422]
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
    body: ListDirectoryRequest,
) -> ListDirectoryResponse | ListDirectoryResponse422 | None:
    """List files and directories at a given path within a project workspace.

     List files and directories at a given path within a project workspace.

    Args:
        body (ListDirectoryRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListDirectoryResponse | ListDirectoryResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListDirectoryRequest,
) -> Response[ListDirectoryResponse | ListDirectoryResponse422]:
    """List files and directories at a given path within a project workspace.

     List files and directories at a given path within a project workspace.

    Args:
        body (ListDirectoryRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListDirectoryResponse | ListDirectoryResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ListDirectoryRequest,
) -> ListDirectoryResponse | ListDirectoryResponse422 | None:
    """List files and directories at a given path within a project workspace.

     List files and directories at a given path within a project workspace.

    Args:
        body (ListDirectoryRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListDirectoryResponse | ListDirectoryResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
