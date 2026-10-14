from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.workspace_cutover_request import WorkspaceCutoverRequest
from ...models.workspace_cutover_response_422 import WorkspaceCutoverResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: WorkspaceCutoverRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/project/workspace-cutover",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | WorkspaceCutoverResponse422 | None:
    if response.status_code == 200:
        response_200 = response.json()
        return response_200

    if response.status_code == 422:
        response_422 = WorkspaceCutoverResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Any | WorkspaceCutoverResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceCutoverRequest,
) -> Response[Any | WorkspaceCutoverResponse422]:
    """Preview or apply a source-preserving base switch. Apply requires the exact preview proof. Disables
    the old base and slots without moving or deleting any source or releasing writers.

     Preview or apply a source-preserving base switch. Apply requires the exact preview proof. Disables
    the old base and slots without moving or deleting any source or releasing writers.

    Args:
        body (WorkspaceCutoverRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | WorkspaceCutoverResponse422]
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
    body: WorkspaceCutoverRequest,
) -> Any | WorkspaceCutoverResponse422 | None:
    """Preview or apply a source-preserving base switch. Apply requires the exact preview proof. Disables
    the old base and slots without moving or deleting any source or releasing writers.

     Preview or apply a source-preserving base switch. Apply requires the exact preview proof. Disables
    the old base and slots without moving or deleting any source or releasing writers.

    Args:
        body (WorkspaceCutoverRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | WorkspaceCutoverResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceCutoverRequest,
) -> Response[Any | WorkspaceCutoverResponse422]:
    """Preview or apply a source-preserving base switch. Apply requires the exact preview proof. Disables
    the old base and slots without moving or deleting any source or releasing writers.

     Preview or apply a source-preserving base switch. Apply requires the exact preview proof. Disables
    the old base and slots without moving or deleting any source or releasing writers.

    Args:
        body (WorkspaceCutoverRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | WorkspaceCutoverResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceCutoverRequest,
) -> Any | WorkspaceCutoverResponse422 | None:
    """Preview or apply a source-preserving base switch. Apply requires the exact preview proof. Disables
    the old base and slots without moving or deleting any source or releasing writers.

     Preview or apply a source-preserving base switch. Apply requires the exact preview proof. Disables
    the old base and slots without moving or deleting any source or releasing writers.

    Args:
        body (WorkspaceCutoverRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | WorkspaceCutoverResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
