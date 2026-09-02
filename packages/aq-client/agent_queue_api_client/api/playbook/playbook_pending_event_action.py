from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_pending_event_action_request import PlaybookPendingEventActionRequest
from ...models.playbook_pending_event_action_response import PlaybookPendingEventActionResponse
from ...models.playbook_pending_event_action_response_422 import PlaybookPendingEventActionResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookPendingEventActionRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/pending-event-action",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookPendingEventActionResponse | PlaybookPendingEventActionResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookPendingEventActionResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookPendingEventActionResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookPendingEventActionResponse | PlaybookPendingEventActionResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookPendingEventActionRequest,
) -> Response[PlaybookPendingEventActionResponse | PlaybookPendingEventActionResponse422]:
    """Dispatch or discard held playbook pending events. 'dispatch' re-enters the engine's own event
    dispatch with the server-derived principal of this request -- it never re-implements matching and
    never adopts a principal from the stored event. 'discard' records the resolution without
    dispatching.

     Dispatch or discard held playbook pending events. 'dispatch' re-enters the engine's own event
    dispatch with the server-derived principal of this request -- it never re-implements matching and
    never adopts a principal from the stored event. 'discard' records the resolution without
    dispatching.

    Args:
        body (PlaybookPendingEventActionRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookPendingEventActionResponse | PlaybookPendingEventActionResponse422]
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
    body: PlaybookPendingEventActionRequest,
) -> PlaybookPendingEventActionResponse | PlaybookPendingEventActionResponse422 | None:
    """Dispatch or discard held playbook pending events. 'dispatch' re-enters the engine's own event
    dispatch with the server-derived principal of this request -- it never re-implements matching and
    never adopts a principal from the stored event. 'discard' records the resolution without
    dispatching.

     Dispatch or discard held playbook pending events. 'dispatch' re-enters the engine's own event
    dispatch with the server-derived principal of this request -- it never re-implements matching and
    never adopts a principal from the stored event. 'discard' records the resolution without
    dispatching.

    Args:
        body (PlaybookPendingEventActionRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookPendingEventActionResponse | PlaybookPendingEventActionResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookPendingEventActionRequest,
) -> Response[PlaybookPendingEventActionResponse | PlaybookPendingEventActionResponse422]:
    """Dispatch or discard held playbook pending events. 'dispatch' re-enters the engine's own event
    dispatch with the server-derived principal of this request -- it never re-implements matching and
    never adopts a principal from the stored event. 'discard' records the resolution without
    dispatching.

     Dispatch or discard held playbook pending events. 'dispatch' re-enters the engine's own event
    dispatch with the server-derived principal of this request -- it never re-implements matching and
    never adopts a principal from the stored event. 'discard' records the resolution without
    dispatching.

    Args:
        body (PlaybookPendingEventActionRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookPendingEventActionResponse | PlaybookPendingEventActionResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookPendingEventActionRequest,
) -> PlaybookPendingEventActionResponse | PlaybookPendingEventActionResponse422 | None:
    """Dispatch or discard held playbook pending events. 'dispatch' re-enters the engine's own event
    dispatch with the server-derived principal of this request -- it never re-implements matching and
    never adopts a principal from the stored event. 'discard' records the resolution without
    dispatching.

     Dispatch or discard held playbook pending events. 'dispatch' re-enters the engine's own event
    dispatch with the server-derived principal of this request -- it never re-implements matching and
    never adopts a principal from the stored event. 'discard' records the resolution without
    dispatching.

    Args:
        body (PlaybookPendingEventActionRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookPendingEventActionResponse | PlaybookPendingEventActionResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
