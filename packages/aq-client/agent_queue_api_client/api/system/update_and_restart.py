from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.update_and_restart_request import UpdateAndRestartRequest
from ...models.update_and_restart_response import UpdateAndRestartResponse
from ...models.update_and_restart_response_422 import UpdateAndRestartResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: UpdateAndRestartRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/update-and-restart",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> UpdateAndRestartResponse | UpdateAndRestartResponse422 | None:
    if response.status_code == 200:
        response_200 = UpdateAndRestartResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = UpdateAndRestartResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[UpdateAndRestartResponse | UpdateAndRestartResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: UpdateAndRestartRequest,
) -> Response[UpdateAndRestartResponse | UpdateAndRestartResponse422]:
    """Pull the latest source from git and restart the daemon. Excluded from MCP by default for safety.

     Pull the latest source from git and restart the daemon. Excluded from MCP by default for safety.

    Args:
        body (UpdateAndRestartRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[UpdateAndRestartResponse | UpdateAndRestartResponse422]
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
    body: UpdateAndRestartRequest,
) -> UpdateAndRestartResponse | UpdateAndRestartResponse422 | None:
    """Pull the latest source from git and restart the daemon. Excluded from MCP by default for safety.

     Pull the latest source from git and restart the daemon. Excluded from MCP by default for safety.

    Args:
        body (UpdateAndRestartRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        UpdateAndRestartResponse | UpdateAndRestartResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: UpdateAndRestartRequest,
) -> Response[UpdateAndRestartResponse | UpdateAndRestartResponse422]:
    """Pull the latest source from git and restart the daemon. Excluded from MCP by default for safety.

     Pull the latest source from git and restart the daemon. Excluded from MCP by default for safety.

    Args:
        body (UpdateAndRestartRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[UpdateAndRestartResponse | UpdateAndRestartResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: UpdateAndRestartRequest,
) -> UpdateAndRestartResponse | UpdateAndRestartResponse422 | None:
    """Pull the latest source from git and restart the daemon. Excluded from MCP by default for safety.

     Pull the latest source from git and restart the daemon. Excluded from MCP by default for safety.

    Args:
        body (UpdateAndRestartRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        UpdateAndRestartResponse | UpdateAndRestartResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
