from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_source_resync_request import PlaybookSourceResyncRequest
from ...models.playbook_source_resync_response import PlaybookSourceResyncResponse
from ...models.playbook_source_resync_response_422 import PlaybookSourceResyncResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookSourceResyncRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/source-resync",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookSourceResyncResponse | PlaybookSourceResyncResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookSourceResyncResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookSourceResyncResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookSourceResyncResponse | PlaybookSourceResyncResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookSourceResyncRequest,
) -> Response[PlaybookSourceResyncResponse | PlaybookSourceResyncResponse422]:
    """Supervisor control: restore system Markdown from the digest-verified source retained with its active
    reviewed artifact, preserving the old bytes in a backup. Requires the active artifact hash and
    current source hash; never compiles or activates.

     Supervisor control: restore system Markdown from the digest-verified source retained with its active
    reviewed artifact, preserving the old bytes in a backup. Requires the active artifact hash and
    current source hash; never compiles or activates.

    Args:
        body (PlaybookSourceResyncRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookSourceResyncResponse | PlaybookSourceResyncResponse422]
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
    body: PlaybookSourceResyncRequest,
) -> PlaybookSourceResyncResponse | PlaybookSourceResyncResponse422 | None:
    """Supervisor control: restore system Markdown from the digest-verified source retained with its active
    reviewed artifact, preserving the old bytes in a backup. Requires the active artifact hash and
    current source hash; never compiles or activates.

     Supervisor control: restore system Markdown from the digest-verified source retained with its active
    reviewed artifact, preserving the old bytes in a backup. Requires the active artifact hash and
    current source hash; never compiles or activates.

    Args:
        body (PlaybookSourceResyncRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookSourceResyncResponse | PlaybookSourceResyncResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookSourceResyncRequest,
) -> Response[PlaybookSourceResyncResponse | PlaybookSourceResyncResponse422]:
    """Supervisor control: restore system Markdown from the digest-verified source retained with its active
    reviewed artifact, preserving the old bytes in a backup. Requires the active artifact hash and
    current source hash; never compiles or activates.

     Supervisor control: restore system Markdown from the digest-verified source retained with its active
    reviewed artifact, preserving the old bytes in a backup. Requires the active artifact hash and
    current source hash; never compiles or activates.

    Args:
        body (PlaybookSourceResyncRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookSourceResyncResponse | PlaybookSourceResyncResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookSourceResyncRequest,
) -> PlaybookSourceResyncResponse | PlaybookSourceResyncResponse422 | None:
    """Supervisor control: restore system Markdown from the digest-verified source retained with its active
    reviewed artifact, preserving the old bytes in a backup. Requires the active artifact hash and
    current source hash; never compiles or activates.

     Supervisor control: restore system Markdown from the digest-verified source retained with its active
    reviewed artifact, preserving the old bytes in a backup. Requires the active artifact hash and
    current source hash; never compiles or activates.

    Args:
        body (PlaybookSourceResyncRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookSourceResyncResponse | PlaybookSourceResyncResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
