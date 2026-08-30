from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_graph_view_request import PlaybookGraphViewRequest
from ...models.playbook_graph_view_response import PlaybookGraphViewResponse
from ...models.playbook_graph_view_response_422 import PlaybookGraphViewResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookGraphViewRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/graph-view",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookGraphViewResponse | PlaybookGraphViewResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookGraphViewResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookGraphViewResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookGraphViewResponse | PlaybookGraphViewResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookGraphViewRequest,
) -> Response[PlaybookGraphViewResponse | PlaybookGraphViewResponse422]:
    """Get structured graph view data for dashboard rendering of a playbook. Returns nodes as positioned
    boxes (color-coded by type), transitions as labelled arrows, with optional overlays for live state
    (current node highlighting for running instances), run path highlighting, and per-node health
    metrics. Suitable for interactive visualization.

     Get structured graph view data for dashboard rendering of a playbook. Returns nodes as positioned
    boxes (color-coded by type), transitions as labelled arrows, with optional overlays for live state
    (current node highlighting for running instances), run path highlighting, and per-node health
    metrics. Suitable for interactive visualization.

    Args:
        body (PlaybookGraphViewRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookGraphViewResponse | PlaybookGraphViewResponse422]
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
    body: PlaybookGraphViewRequest,
) -> PlaybookGraphViewResponse | PlaybookGraphViewResponse422 | None:
    """Get structured graph view data for dashboard rendering of a playbook. Returns nodes as positioned
    boxes (color-coded by type), transitions as labelled arrows, with optional overlays for live state
    (current node highlighting for running instances), run path highlighting, and per-node health
    metrics. Suitable for interactive visualization.

     Get structured graph view data for dashboard rendering of a playbook. Returns nodes as positioned
    boxes (color-coded by type), transitions as labelled arrows, with optional overlays for live state
    (current node highlighting for running instances), run path highlighting, and per-node health
    metrics. Suitable for interactive visualization.

    Args:
        body (PlaybookGraphViewRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookGraphViewResponse | PlaybookGraphViewResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookGraphViewRequest,
) -> Response[PlaybookGraphViewResponse | PlaybookGraphViewResponse422]:
    """Get structured graph view data for dashboard rendering of a playbook. Returns nodes as positioned
    boxes (color-coded by type), transitions as labelled arrows, with optional overlays for live state
    (current node highlighting for running instances), run path highlighting, and per-node health
    metrics. Suitable for interactive visualization.

     Get structured graph view data for dashboard rendering of a playbook. Returns nodes as positioned
    boxes (color-coded by type), transitions as labelled arrows, with optional overlays for live state
    (current node highlighting for running instances), run path highlighting, and per-node health
    metrics. Suitable for interactive visualization.

    Args:
        body (PlaybookGraphViewRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookGraphViewResponse | PlaybookGraphViewResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookGraphViewRequest,
) -> PlaybookGraphViewResponse | PlaybookGraphViewResponse422 | None:
    """Get structured graph view data for dashboard rendering of a playbook. Returns nodes as positioned
    boxes (color-coded by type), transitions as labelled arrows, with optional overlays for live state
    (current node highlighting for running instances), run path highlighting, and per-node health
    metrics. Suitable for interactive visualization.

     Get structured graph view data for dashboard rendering of a playbook. Returns nodes as positioned
    boxes (color-coded by type), transitions as labelled arrows, with optional overlays for live state
    (current node highlighting for running instances), run path highlighting, and per-node health
    metrics. Suitable for interactive visualization.

    Args:
        body (PlaybookGraphViewRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookGraphViewResponse | PlaybookGraphViewResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
