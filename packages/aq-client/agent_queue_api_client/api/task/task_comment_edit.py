from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.task_comment_edit_request import TaskCommentEditRequest
from ...models.task_comment_edit_response_422 import TaskCommentEditResponse422
from ...models.task_comment_response import TaskCommentResponse
from ...types import Response


def _get_kwargs(
    *,
    body: TaskCommentEditRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/comment-edit",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> TaskCommentEditResponse422 | TaskCommentResponse | None:
    if response.status_code == 200:
        response_200 = TaskCommentResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = TaskCommentEditResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[TaskCommentEditResponse422 | TaskCommentResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskCommentEditRequest,
) -> Response[TaskCommentEditResponse422 | TaskCommentResponse]:
    """Replace the text of an existing task comment. Operator surfaces only: agent sessions are append-
    only. Author and timestamp are preserved.

     Replace the text of an existing task comment. Operator surfaces only: agent sessions are append-
    only. Author and timestamp are preserved.

    Args:
        body (TaskCommentEditRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskCommentEditResponse422 | TaskCommentResponse]
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
    body: TaskCommentEditRequest,
) -> TaskCommentEditResponse422 | TaskCommentResponse | None:
    """Replace the text of an existing task comment. Operator surfaces only: agent sessions are append-
    only. Author and timestamp are preserved.

     Replace the text of an existing task comment. Operator surfaces only: agent sessions are append-
    only. Author and timestamp are preserved.

    Args:
        body (TaskCommentEditRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskCommentEditResponse422 | TaskCommentResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskCommentEditRequest,
) -> Response[TaskCommentEditResponse422 | TaskCommentResponse]:
    """Replace the text of an existing task comment. Operator surfaces only: agent sessions are append-
    only. Author and timestamp are preserved.

     Replace the text of an existing task comment. Operator surfaces only: agent sessions are append-
    only. Author and timestamp are preserved.

    Args:
        body (TaskCommentEditRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskCommentEditResponse422 | TaskCommentResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: TaskCommentEditRequest,
) -> TaskCommentEditResponse422 | TaskCommentResponse | None:
    """Replace the text of an existing task comment. Operator surfaces only: agent sessions are append-
    only. Author and timestamp are preserved.

     Replace the text of an existing task comment. Operator surfaces only: agent sessions are append-
    only. Author and timestamp are preserved.

    Args:
        body (TaskCommentEditRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskCommentEditResponse422 | TaskCommentResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
