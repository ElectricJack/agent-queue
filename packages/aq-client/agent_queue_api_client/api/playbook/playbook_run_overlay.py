from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_run_overlay_request import PlaybookRunOverlayRequest
from ...models.playbook_run_overlay_response import PlaybookRunOverlayResponse
from ...models.playbook_run_overlay_response_422 import PlaybookRunOverlayResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookRunOverlayRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/run-overlay",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookRunOverlayResponse | PlaybookRunOverlayResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookRunOverlayResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookRunOverlayResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookRunOverlayResponse | PlaybookRunOverlayResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookRunOverlayRequest,
) -> Response[PlaybookRunOverlayResponse | PlaybookRunOverlayResponse422]:
    """Return one Playbook V2 run's execution overlay, pinned to the artifact the run actually executed --
    never the playbook's current activation, so an overlay is never projected onto a newer artifact.
    artifact_is_active=false is how a viewer knows the run used an older artifact. Loop iterations are
    listed individually rather than collapsed into one misleading status.

     Return one Playbook V2 run's execution overlay, pinned to the artifact the run actually executed --
    never the playbook's current activation, so an overlay is never projected onto a newer artifact.
    artifact_is_active=false is how a viewer knows the run used an older artifact. Loop iterations are
    listed individually rather than collapsed into one misleading status.

    Args:
        body (PlaybookRunOverlayRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookRunOverlayResponse | PlaybookRunOverlayResponse422]
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
    body: PlaybookRunOverlayRequest,
) -> PlaybookRunOverlayResponse | PlaybookRunOverlayResponse422 | None:
    """Return one Playbook V2 run's execution overlay, pinned to the artifact the run actually executed --
    never the playbook's current activation, so an overlay is never projected onto a newer artifact.
    artifact_is_active=false is how a viewer knows the run used an older artifact. Loop iterations are
    listed individually rather than collapsed into one misleading status.

     Return one Playbook V2 run's execution overlay, pinned to the artifact the run actually executed --
    never the playbook's current activation, so an overlay is never projected onto a newer artifact.
    artifact_is_active=false is how a viewer knows the run used an older artifact. Loop iterations are
    listed individually rather than collapsed into one misleading status.

    Args:
        body (PlaybookRunOverlayRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookRunOverlayResponse | PlaybookRunOverlayResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookRunOverlayRequest,
) -> Response[PlaybookRunOverlayResponse | PlaybookRunOverlayResponse422]:
    """Return one Playbook V2 run's execution overlay, pinned to the artifact the run actually executed --
    never the playbook's current activation, so an overlay is never projected onto a newer artifact.
    artifact_is_active=false is how a viewer knows the run used an older artifact. Loop iterations are
    listed individually rather than collapsed into one misleading status.

     Return one Playbook V2 run's execution overlay, pinned to the artifact the run actually executed --
    never the playbook's current activation, so an overlay is never projected onto a newer artifact.
    artifact_is_active=false is how a viewer knows the run used an older artifact. Loop iterations are
    listed individually rather than collapsed into one misleading status.

    Args:
        body (PlaybookRunOverlayRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookRunOverlayResponse | PlaybookRunOverlayResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookRunOverlayRequest,
) -> PlaybookRunOverlayResponse | PlaybookRunOverlayResponse422 | None:
    """Return one Playbook V2 run's execution overlay, pinned to the artifact the run actually executed --
    never the playbook's current activation, so an overlay is never projected onto a newer artifact.
    artifact_is_active=false is how a viewer knows the run used an older artifact. Loop iterations are
    listed individually rather than collapsed into one misleading status.

     Return one Playbook V2 run's execution overlay, pinned to the artifact the run actually executed --
    never the playbook's current activation, so an overlay is never projected onto a newer artifact.
    artifact_is_active=false is how a viewer knows the run used an older artifact. Loop iterations are
    listed individually rather than collapsed into one misleading status.

    Args:
        body (PlaybookRunOverlayRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookRunOverlayResponse | PlaybookRunOverlayResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
