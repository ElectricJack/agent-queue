from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.wait_bridge_response import WaitBridgeResponse
from ...models.wait_legacy_bridge_args import WaitLegacyBridgeArgs
from ...models.wait_legacy_bridge_response_422 import WaitLegacyBridgeResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: WaitLegacyBridgeArgs,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/wait/legacy-bridge",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> WaitBridgeResponse | WaitLegacyBridgeResponse422 | None:
    if response.status_code == 200:
        response_200 = WaitBridgeResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = WaitLegacyBridgeResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[WaitBridgeResponse | WaitLegacyBridgeResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: WaitLegacyBridgeArgs,
) -> Response[WaitBridgeResponse | WaitLegacyBridgeResponse422]:
    """Wait legacy bridge through verified wait state.

     Wait legacy bridge through verified wait state.

    Args:
        body (WaitLegacyBridgeArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[WaitBridgeResponse | WaitLegacyBridgeResponse422]
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
    body: WaitLegacyBridgeArgs,
) -> WaitBridgeResponse | WaitLegacyBridgeResponse422 | None:
    """Wait legacy bridge through verified wait state.

     Wait legacy bridge through verified wait state.

    Args:
        body (WaitLegacyBridgeArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        WaitBridgeResponse | WaitLegacyBridgeResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: WaitLegacyBridgeArgs,
) -> Response[WaitBridgeResponse | WaitLegacyBridgeResponse422]:
    """Wait legacy bridge through verified wait state.

     Wait legacy bridge through verified wait state.

    Args:
        body (WaitLegacyBridgeArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[WaitBridgeResponse | WaitLegacyBridgeResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: WaitLegacyBridgeArgs,
) -> WaitBridgeResponse | WaitLegacyBridgeResponse422 | None:
    """Wait legacy bridge through verified wait state.

     Wait legacy bridge through verified wait state.

    Args:
        body (WaitLegacyBridgeArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        WaitBridgeResponse | WaitLegacyBridgeResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
