from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.reopen_with_feedback_request import ReopenWithFeedbackRequest
from ...models.reopen_with_feedback_response import ReopenWithFeedbackResponse
from ...models.reopen_with_feedback_response_422 import ReopenWithFeedbackResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ReopenWithFeedbackRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/reopen-with-feedback",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ReopenWithFeedbackResponse | ReopenWithFeedbackResponse422 | None:
    if response.status_code == 200:
        response_200 = ReopenWithFeedbackResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ReopenWithFeedbackResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ReopenWithFeedbackResponse | ReopenWithFeedbackResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ReopenWithFeedbackRequest,
) -> Response[ReopenWithFeedbackResponse | ReopenWithFeedbackResponse422]:
    """Reopen a completed or failed task with feedback. Use this when a task needs rework — the feedback is
    appended to the task description and stored as a structured context entry so the agent sees it on
    re-execution. The task is reset to READY, retry count is cleared, and the PR URL is removed so a
    fresh PR can be created.

     Reopen a completed or failed task with feedback. Use this when a task needs rework — the feedback is
    appended to the task description and stored as a structured context entry so the agent sees it on
    re-execution. The task is reset to READY, retry count is cleared, and the PR URL is removed so a
    fresh PR can be created.

    Args:
        body (ReopenWithFeedbackRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ReopenWithFeedbackResponse | ReopenWithFeedbackResponse422]
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
    body: ReopenWithFeedbackRequest,
) -> ReopenWithFeedbackResponse | ReopenWithFeedbackResponse422 | None:
    """Reopen a completed or failed task with feedback. Use this when a task needs rework — the feedback is
    appended to the task description and stored as a structured context entry so the agent sees it on
    re-execution. The task is reset to READY, retry count is cleared, and the PR URL is removed so a
    fresh PR can be created.

     Reopen a completed or failed task with feedback. Use this when a task needs rework — the feedback is
    appended to the task description and stored as a structured context entry so the agent sees it on
    re-execution. The task is reset to READY, retry count is cleared, and the PR URL is removed so a
    fresh PR can be created.

    Args:
        body (ReopenWithFeedbackRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ReopenWithFeedbackResponse | ReopenWithFeedbackResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ReopenWithFeedbackRequest,
) -> Response[ReopenWithFeedbackResponse | ReopenWithFeedbackResponse422]:
    """Reopen a completed or failed task with feedback. Use this when a task needs rework — the feedback is
    appended to the task description and stored as a structured context entry so the agent sees it on
    re-execution. The task is reset to READY, retry count is cleared, and the PR URL is removed so a
    fresh PR can be created.

     Reopen a completed or failed task with feedback. Use this when a task needs rework — the feedback is
    appended to the task description and stored as a structured context entry so the agent sees it on
    re-execution. The task is reset to READY, retry count is cleared, and the PR URL is removed so a
    fresh PR can be created.

    Args:
        body (ReopenWithFeedbackRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ReopenWithFeedbackResponse | ReopenWithFeedbackResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ReopenWithFeedbackRequest,
) -> ReopenWithFeedbackResponse | ReopenWithFeedbackResponse422 | None:
    """Reopen a completed or failed task with feedback. Use this when a task needs rework — the feedback is
    appended to the task description and stored as a structured context entry so the agent sees it on
    re-execution. The task is reset to READY, retry count is cleared, and the PR URL is removed so a
    fresh PR can be created.

     Reopen a completed or failed task with feedback. Use this when a task needs rework — the feedback is
    appended to the task description and stored as a structured context entry so the agent sees it on
    re-execution. The task is reset to READY, retry count is cleared, and the PR URL is removed so a
    fresh PR can be created.

    Args:
        body (ReopenWithFeedbackRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ReopenWithFeedbackResponse | ReopenWithFeedbackResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
