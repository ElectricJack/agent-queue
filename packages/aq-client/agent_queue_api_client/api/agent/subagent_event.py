from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.subagent_event_request import SubagentEventRequest
from ...models.subagent_event_response import SubagentEventResponse
from ...models.subagent_event_response_422 import SubagentEventResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: SubagentEventRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/agent/subagent-event",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> SubagentEventResponse | SubagentEventResponse422 | None:
    if response.status_code == 200:
        response_200 = SubagentEventResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = SubagentEventResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[SubagentEventResponse | SubagentEventResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SubagentEventRequest,
) -> Response[SubagentEventResponse | SubagentEventResponse422]:
    """Record a native sub-agent start or stop event for the calling session. Harness hooks use this to
    report their own child-agent lifecycle; the session identity comes from the bearer token when
    present.

     Record a native sub-agent start or stop event for the calling session. Harness hooks use this to
    report their own child-agent lifecycle; the session identity comes from the bearer token when
    present.

    Args:
        body (SubagentEventRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[SubagentEventResponse | SubagentEventResponse422]
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
    body: SubagentEventRequest,
) -> SubagentEventResponse | SubagentEventResponse422 | None:
    """Record a native sub-agent start or stop event for the calling session. Harness hooks use this to
    report their own child-agent lifecycle; the session identity comes from the bearer token when
    present.

     Record a native sub-agent start or stop event for the calling session. Harness hooks use this to
    report their own child-agent lifecycle; the session identity comes from the bearer token when
    present.

    Args:
        body (SubagentEventRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        SubagentEventResponse | SubagentEventResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SubagentEventRequest,
) -> Response[SubagentEventResponse | SubagentEventResponse422]:
    """Record a native sub-agent start or stop event for the calling session. Harness hooks use this to
    report their own child-agent lifecycle; the session identity comes from the bearer token when
    present.

     Record a native sub-agent start or stop event for the calling session. Harness hooks use this to
    report their own child-agent lifecycle; the session identity comes from the bearer token when
    present.

    Args:
        body (SubagentEventRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[SubagentEventResponse | SubagentEventResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: SubagentEventRequest,
) -> SubagentEventResponse | SubagentEventResponse422 | None:
    """Record a native sub-agent start or stop event for the calling session. Harness hooks use this to
    report their own child-agent lifecycle; the session identity comes from the bearer token when
    present.

     Record a native sub-agent start or stop event for the calling session. Harness hooks use this to
    report their own child-agent lifecycle; the session identity comes from the bearer token when
    present.

    Args:
        body (SubagentEventRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        SubagentEventResponse | SubagentEventResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
