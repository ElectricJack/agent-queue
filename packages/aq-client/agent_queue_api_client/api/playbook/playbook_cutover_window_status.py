from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_cutover_window_status_request import PlaybookCutoverWindowStatusRequest
from ...models.playbook_cutover_window_status_response import PlaybookCutoverWindowStatusResponse
from ...models.playbook_cutover_window_status_response_422 import PlaybookCutoverWindowStatusResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookCutoverWindowStatusRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/cutover-window-status",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookCutoverWindowStatusResponse | PlaybookCutoverWindowStatusResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookCutoverWindowStatusResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookCutoverWindowStatusResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookCutoverWindowStatusResponse | PlaybookCutoverWindowStatusResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookCutoverWindowStatusRequest,
) -> Response[PlaybookCutoverWindowStatusResponse | PlaybookCutoverWindowStatusResponse422]:
    """Measure the rollback observation window and the cutover acceptance table. Read-only, and recomputed
    from source on every call -- it never reads a cached verdict. Every measure names its evidence
    source, what was observed and when; a source that cannot be read is reported as unreadable and fails
    the measures it feeds, never as fine. The window needs 72h since the switch, one V2 run per enabled
    playbook, and 200 V2 runs in total.

     Measure the rollback observation window and the cutover acceptance table. Read-only, and recomputed
    from source on every call -- it never reads a cached verdict. Every measure names its evidence
    source, what was observed and when; a source that cannot be read is reported as unreadable and fails
    the measures it feeds, never as fine. The window needs 72h since the switch, one V2 run per enabled
    playbook, and 200 V2 runs in total.

    Args:
        body (PlaybookCutoverWindowStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookCutoverWindowStatusResponse | PlaybookCutoverWindowStatusResponse422]
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
    body: PlaybookCutoverWindowStatusRequest,
) -> PlaybookCutoverWindowStatusResponse | PlaybookCutoverWindowStatusResponse422 | None:
    """Measure the rollback observation window and the cutover acceptance table. Read-only, and recomputed
    from source on every call -- it never reads a cached verdict. Every measure names its evidence
    source, what was observed and when; a source that cannot be read is reported as unreadable and fails
    the measures it feeds, never as fine. The window needs 72h since the switch, one V2 run per enabled
    playbook, and 200 V2 runs in total.

     Measure the rollback observation window and the cutover acceptance table. Read-only, and recomputed
    from source on every call -- it never reads a cached verdict. Every measure names its evidence
    source, what was observed and when; a source that cannot be read is reported as unreadable and fails
    the measures it feeds, never as fine. The window needs 72h since the switch, one V2 run per enabled
    playbook, and 200 V2 runs in total.

    Args:
        body (PlaybookCutoverWindowStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookCutoverWindowStatusResponse | PlaybookCutoverWindowStatusResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookCutoverWindowStatusRequest,
) -> Response[PlaybookCutoverWindowStatusResponse | PlaybookCutoverWindowStatusResponse422]:
    """Measure the rollback observation window and the cutover acceptance table. Read-only, and recomputed
    from source on every call -- it never reads a cached verdict. Every measure names its evidence
    source, what was observed and when; a source that cannot be read is reported as unreadable and fails
    the measures it feeds, never as fine. The window needs 72h since the switch, one V2 run per enabled
    playbook, and 200 V2 runs in total.

     Measure the rollback observation window and the cutover acceptance table. Read-only, and recomputed
    from source on every call -- it never reads a cached verdict. Every measure names its evidence
    source, what was observed and when; a source that cannot be read is reported as unreadable and fails
    the measures it feeds, never as fine. The window needs 72h since the switch, one V2 run per enabled
    playbook, and 200 V2 runs in total.

    Args:
        body (PlaybookCutoverWindowStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookCutoverWindowStatusResponse | PlaybookCutoverWindowStatusResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookCutoverWindowStatusRequest,
) -> PlaybookCutoverWindowStatusResponse | PlaybookCutoverWindowStatusResponse422 | None:
    """Measure the rollback observation window and the cutover acceptance table. Read-only, and recomputed
    from source on every call -- it never reads a cached verdict. Every measure names its evidence
    source, what was observed and when; a source that cannot be read is reported as unreadable and fails
    the measures it feeds, never as fine. The window needs 72h since the switch, one V2 run per enabled
    playbook, and 200 V2 runs in total.

     Measure the rollback observation window and the cutover acceptance table. Read-only, and recomputed
    from source on every call -- it never reads a cached verdict. Every measure names its evidence
    source, what was observed and when; a source that cannot be read is reported as unreadable and fails
    the measures it feeds, never as fine. The window needs 72h since the switch, one V2 run per enabled
    playbook, and 200 V2 runs in total.

    Args:
        body (PlaybookCutoverWindowStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookCutoverWindowStatusResponse | PlaybookCutoverWindowStatusResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
