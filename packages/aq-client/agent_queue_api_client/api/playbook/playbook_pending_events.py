from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.list_playbook_pending_events_response import ListPlaybookPendingEventsResponse
from ...models.playbook_pending_events_request import PlaybookPendingEventsRequest
from ...models.playbook_pending_events_response_422 import PlaybookPendingEventsResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookPendingEventsRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/pending-events",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ListPlaybookPendingEventsResponse | PlaybookPendingEventsResponse422 | None:
    if response.status_code == 200:
        response_200 = ListPlaybookPendingEventsResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookPendingEventsResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ListPlaybookPendingEventsResponse | PlaybookPendingEventsResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookPendingEventsRequest,
) -> Response[ListPlaybookPendingEventsResponse | PlaybookPendingEventsResponse422]:
    """List events held because no artifact could run them -- a stale contract, an invalid artifact, a
    disabled activation, an unavailable artifact file, or an unanswered compile question. Pending events
    are retained, visible and operable; they are never silently dropped.

     List events held because no artifact could run them -- a stale contract, an invalid artifact, a
    disabled activation, an unavailable artifact file, or an unanswered compile question. Pending events
    are retained, visible and operable; they are never silently dropped.

    Args:
        body (PlaybookPendingEventsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListPlaybookPendingEventsResponse | PlaybookPendingEventsResponse422]
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
    body: PlaybookPendingEventsRequest,
) -> ListPlaybookPendingEventsResponse | PlaybookPendingEventsResponse422 | None:
    """List events held because no artifact could run them -- a stale contract, an invalid artifact, a
    disabled activation, an unavailable artifact file, or an unanswered compile question. Pending events
    are retained, visible and operable; they are never silently dropped.

     List events held because no artifact could run them -- a stale contract, an invalid artifact, a
    disabled activation, an unavailable artifact file, or an unanswered compile question. Pending events
    are retained, visible and operable; they are never silently dropped.

    Args:
        body (PlaybookPendingEventsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListPlaybookPendingEventsResponse | PlaybookPendingEventsResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookPendingEventsRequest,
) -> Response[ListPlaybookPendingEventsResponse | PlaybookPendingEventsResponse422]:
    """List events held because no artifact could run them -- a stale contract, an invalid artifact, a
    disabled activation, an unavailable artifact file, or an unanswered compile question. Pending events
    are retained, visible and operable; they are never silently dropped.

     List events held because no artifact could run them -- a stale contract, an invalid artifact, a
    disabled activation, an unavailable artifact file, or an unanswered compile question. Pending events
    are retained, visible and operable; they are never silently dropped.

    Args:
        body (PlaybookPendingEventsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListPlaybookPendingEventsResponse | PlaybookPendingEventsResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookPendingEventsRequest,
) -> ListPlaybookPendingEventsResponse | PlaybookPendingEventsResponse422 | None:
    """List events held because no artifact could run them -- a stale contract, an invalid artifact, a
    disabled activation, an unavailable artifact file, or an unanswered compile question. Pending events
    are retained, visible and operable; they are never silently dropped.

     List events held because no artifact could run them -- a stale contract, an invalid artifact, a
    disabled activation, an unavailable artifact file, or an unanswered compile question. Pending events
    are retained, visible and operable; they are never silently dropped.

    Args:
        body (PlaybookPendingEventsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListPlaybookPendingEventsResponse | PlaybookPendingEventsResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
