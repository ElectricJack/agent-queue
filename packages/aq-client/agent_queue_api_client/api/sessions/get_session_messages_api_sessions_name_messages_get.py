from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.get_session_messages_api_sessions_name_messages_get_response_get_session_messages_api_sessions_name_messages_get import (
    GetSessionMessagesApiSessionsNameMessagesGetResponseGetSessionMessagesApiSessionsNameMessagesGet,
)
from ...models.http_validation_error import HTTPValidationError
from ...types import UNSET, Response, Unset


def _get_kwargs(
    name: str,
    *,
    thread_id: None | str | Unset = UNSET,
    since: float | None | Unset = UNSET,
    limit: int | Unset = 100,
    include_archived: bool | Unset = False,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_thread_id: None | str | Unset
    if isinstance(thread_id, Unset):
        json_thread_id = UNSET
    else:
        json_thread_id = thread_id
    params["thread_id"] = json_thread_id

    json_since: float | None | Unset
    if isinstance(since, Unset):
        json_since = UNSET
    else:
        json_since = since
    params["since"] = json_since

    params["limit"] = limit

    params["include_archived"] = include_archived

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/sessions/{name}/messages".format(
            name=quote(str(name), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> (
    GetSessionMessagesApiSessionsNameMessagesGetResponseGetSessionMessagesApiSessionsNameMessagesGet
    | HTTPValidationError
    | None
):
    if response.status_code == 200:
        response_200 = (
            GetSessionMessagesApiSessionsNameMessagesGetResponseGetSessionMessagesApiSessionsNameMessagesGet.from_dict(
                response.json()
            )
        )

        return response_200

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[
    GetSessionMessagesApiSessionsNameMessagesGetResponseGetSessionMessagesApiSessionsNameMessagesGet
    | HTTPValidationError
]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    name: str,
    *,
    client: AuthenticatedClient | Client,
    thread_id: None | str | Unset = UNSET,
    since: float | None | Unset = UNSET,
    limit: int | Unset = 100,
    include_archived: bool | Unset = False,
) -> Response[
    GetSessionMessagesApiSessionsNameMessagesGetResponseGetSessionMessagesApiSessionsNameMessagesGet
    | HTTPValidationError
]:
    """Get Session Messages

     Poll the conversation for a session.

    Returns every message in the session's project matching the filters —
    both directions of the conversation — so a poller (``aq chat --once``
    when the WebSocket isn't available) sees the reply as well as its own
    outbound message.

    Args:
        name (str):
        thread_id (None | str | Unset):
        since (float | None | Unset):
        limit (int | Unset):  Default: 100.
        include_archived (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetSessionMessagesApiSessionsNameMessagesGetResponseGetSessionMessagesApiSessionsNameMessagesGet | HTTPValidationError]
    """

    kwargs = _get_kwargs(
        name=name,
        thread_id=thread_id,
        since=since,
        limit=limit,
        include_archived=include_archived,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    name: str,
    *,
    client: AuthenticatedClient | Client,
    thread_id: None | str | Unset = UNSET,
    since: float | None | Unset = UNSET,
    limit: int | Unset = 100,
    include_archived: bool | Unset = False,
) -> (
    GetSessionMessagesApiSessionsNameMessagesGetResponseGetSessionMessagesApiSessionsNameMessagesGet
    | HTTPValidationError
    | None
):
    """Get Session Messages

     Poll the conversation for a session.

    Returns every message in the session's project matching the filters —
    both directions of the conversation — so a poller (``aq chat --once``
    when the WebSocket isn't available) sees the reply as well as its own
    outbound message.

    Args:
        name (str):
        thread_id (None | str | Unset):
        since (float | None | Unset):
        limit (int | Unset):  Default: 100.
        include_archived (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetSessionMessagesApiSessionsNameMessagesGetResponseGetSessionMessagesApiSessionsNameMessagesGet | HTTPValidationError
    """

    return sync_detailed(
        name=name,
        client=client,
        thread_id=thread_id,
        since=since,
        limit=limit,
        include_archived=include_archived,
    ).parsed


async def asyncio_detailed(
    name: str,
    *,
    client: AuthenticatedClient | Client,
    thread_id: None | str | Unset = UNSET,
    since: float | None | Unset = UNSET,
    limit: int | Unset = 100,
    include_archived: bool | Unset = False,
) -> Response[
    GetSessionMessagesApiSessionsNameMessagesGetResponseGetSessionMessagesApiSessionsNameMessagesGet
    | HTTPValidationError
]:
    """Get Session Messages

     Poll the conversation for a session.

    Returns every message in the session's project matching the filters —
    both directions of the conversation — so a poller (``aq chat --once``
    when the WebSocket isn't available) sees the reply as well as its own
    outbound message.

    Args:
        name (str):
        thread_id (None | str | Unset):
        since (float | None | Unset):
        limit (int | Unset):  Default: 100.
        include_archived (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetSessionMessagesApiSessionsNameMessagesGetResponseGetSessionMessagesApiSessionsNameMessagesGet | HTTPValidationError]
    """

    kwargs = _get_kwargs(
        name=name,
        thread_id=thread_id,
        since=since,
        limit=limit,
        include_archived=include_archived,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    name: str,
    *,
    client: AuthenticatedClient | Client,
    thread_id: None | str | Unset = UNSET,
    since: float | None | Unset = UNSET,
    limit: int | Unset = 100,
    include_archived: bool | Unset = False,
) -> (
    GetSessionMessagesApiSessionsNameMessagesGetResponseGetSessionMessagesApiSessionsNameMessagesGet
    | HTTPValidationError
    | None
):
    """Get Session Messages

     Poll the conversation for a session.

    Returns every message in the session's project matching the filters —
    both directions of the conversation — so a poller (``aq chat --once``
    when the WebSocket isn't available) sees the reply as well as its own
    outbound message.

    Args:
        name (str):
        thread_id (None | str | Unset):
        since (float | None | Unset):
        limit (int | Unset):  Default: 100.
        include_archived (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetSessionMessagesApiSessionsNameMessagesGetResponseGetSessionMessagesApiSessionsNameMessagesGet | HTTPValidationError
    """

    return (
        await asyncio_detailed(
            name=name,
            client=client,
            thread_id=thread_id,
            since=since,
            limit=limit,
            include_archived=include_archived,
        )
    ).parsed
