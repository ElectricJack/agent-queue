from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.list_sessions_response import ListSessionsResponse
from ...models.session_list_request import SessionListRequest
from ...models.session_list_response_422 import SessionListResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: SessionListRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/session-list",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ListSessionsResponse | SessionListResponse422 | None:
    if response.status_code == 200:
        response_200 = ListSessionsResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = SessionListResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ListSessionsResponse | SessionListResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SessionListRequest,
) -> Response[ListSessionsResponse | SessionListResponse422]:
    """List sessions with lifecycle, state, task, harness, activity.

     List sessions with lifecycle, state, task, harness, activity.

    Args:
        body (SessionListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListSessionsResponse | SessionListResponse422]
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
    body: SessionListRequest,
) -> ListSessionsResponse | SessionListResponse422 | None:
    """List sessions with lifecycle, state, task, harness, activity.

     List sessions with lifecycle, state, task, harness, activity.

    Args:
        body (SessionListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListSessionsResponse | SessionListResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SessionListRequest,
) -> Response[ListSessionsResponse | SessionListResponse422]:
    """List sessions with lifecycle, state, task, harness, activity.

     List sessions with lifecycle, state, task, harness, activity.

    Args:
        body (SessionListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListSessionsResponse | SessionListResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: SessionListRequest,
) -> ListSessionsResponse | SessionListResponse422 | None:
    """List sessions with lifecycle, state, task, harness, activity.

     List sessions with lifecycle, state, task, harness, activity.

    Args:
        body (SessionListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListSessionsResponse | SessionListResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
