from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.http_validation_error import HTTPValidationError
from ...models.message_send_request import MessageSendRequest
from ...models.post_message_api_messages_send_post_response_post_message_api_messages_send_post import (
    PostMessageApiMessagesSendPostResponsePostMessageApiMessagesSendPost,
)
from ...types import Response


def _get_kwargs(
    *,
    body: MessageSendRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/messages/send",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> HTTPValidationError | PostMessageApiMessagesSendPostResponsePostMessageApiMessagesSendPost | None:
    if response.status_code == 200:
        response_200 = PostMessageApiMessagesSendPostResponsePostMessageApiMessagesSendPost.from_dict(response.json())

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
) -> Response[HTTPValidationError | PostMessageApiMessagesSendPostResponsePostMessageApiMessagesSendPost]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: MessageSendRequest,
) -> Response[HTTPValidationError | PostMessageApiMessagesSendPostResponsePostMessageApiMessagesSendPost]:
    """Post Message

     Queue a message to any supported recipient kind.

    Args:
        body (MessageSendRequest): Body of POST /api/messages/send for non-session recipients.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | PostMessageApiMessagesSendPostResponsePostMessageApiMessagesSendPost]
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
    body: MessageSendRequest,
) -> HTTPValidationError | PostMessageApiMessagesSendPostResponsePostMessageApiMessagesSendPost | None:
    """Post Message

     Queue a message to any supported recipient kind.

    Args:
        body (MessageSendRequest): Body of POST /api/messages/send for non-session recipients.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | PostMessageApiMessagesSendPostResponsePostMessageApiMessagesSendPost
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: MessageSendRequest,
) -> Response[HTTPValidationError | PostMessageApiMessagesSendPostResponsePostMessageApiMessagesSendPost]:
    """Post Message

     Queue a message to any supported recipient kind.

    Args:
        body (MessageSendRequest): Body of POST /api/messages/send for non-session recipients.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | PostMessageApiMessagesSendPostResponsePostMessageApiMessagesSendPost]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: MessageSendRequest,
) -> HTTPValidationError | PostMessageApiMessagesSendPostResponsePostMessageApiMessagesSendPost | None:
    """Post Message

     Queue a message to any supported recipient kind.

    Args:
        body (MessageSendRequest): Body of POST /api/messages/send for non-session recipients.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | PostMessageApiMessagesSendPostResponsePostMessageApiMessagesSendPost
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
