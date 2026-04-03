from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.set_control_interface_request import SetControlInterfaceRequest
from ...models.set_control_interface_response_422 import SetControlInterfaceResponse422
from ...models.set_project_channel_response import SetProjectChannelResponse
from ...types import Response


def _get_kwargs(
    *,
    body: SetControlInterfaceRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/project/set-control-interface",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> SetControlInterfaceResponse422 | SetProjectChannelResponse | None:
    if response.status_code == 200:
        response_200 = SetProjectChannelResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = SetControlInterfaceResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[SetControlInterfaceResponse422 | SetProjectChannelResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SetControlInterfaceRequest,
) -> Response[SetControlInterfaceResponse422 | SetProjectChannelResponse]:
    """Set a project's channel by channel name (string lookup). Resolves the channel name within the guild.
    Deprecated — prefer edit_project with discord_channel_id.

     Set a project's channel by channel name (string lookup). Resolves the channel name within the guild.
    Deprecated — prefer edit_project with discord_channel_id.

    Args:
        body (SetControlInterfaceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[SetControlInterfaceResponse422 | SetProjectChannelResponse]
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
    body: SetControlInterfaceRequest,
) -> SetControlInterfaceResponse422 | SetProjectChannelResponse | None:
    """Set a project's channel by channel name (string lookup). Resolves the channel name within the guild.
    Deprecated — prefer edit_project with discord_channel_id.

     Set a project's channel by channel name (string lookup). Resolves the channel name within the guild.
    Deprecated — prefer edit_project with discord_channel_id.

    Args:
        body (SetControlInterfaceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        SetControlInterfaceResponse422 | SetProjectChannelResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SetControlInterfaceRequest,
) -> Response[SetControlInterfaceResponse422 | SetProjectChannelResponse]:
    """Set a project's channel by channel name (string lookup). Resolves the channel name within the guild.
    Deprecated — prefer edit_project with discord_channel_id.

     Set a project's channel by channel name (string lookup). Resolves the channel name within the guild.
    Deprecated — prefer edit_project with discord_channel_id.

    Args:
        body (SetControlInterfaceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[SetControlInterfaceResponse422 | SetProjectChannelResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: SetControlInterfaceRequest,
) -> SetControlInterfaceResponse422 | SetProjectChannelResponse | None:
    """Set a project's channel by channel name (string lookup). Resolves the channel name within the guild.
    Deprecated — prefer edit_project with discord_channel_id.

     Set a project's channel by channel name (string lookup). Resolves the channel name within the guild.
    Deprecated — prefer edit_project with discord_channel_id.

    Args:
        body (SetControlInterfaceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        SetControlInterfaceResponse422 | SetProjectChannelResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
