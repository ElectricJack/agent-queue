from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.task_recent_activity_request import TaskRecentActivityRequest
from ...models.task_recent_activity_response import TaskRecentActivityResponse
from ...models.task_recent_activity_response_422 import TaskRecentActivityResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: TaskRecentActivityRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/recent-activity",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> TaskRecentActivityResponse | TaskRecentActivityResponse422 | None:
    if response.status_code == 200:
        response_200 = TaskRecentActivityResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = TaskRecentActivityResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[TaskRecentActivityResponse | TaskRecentActivityResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskRecentActivityRequest,
) -> Response[TaskRecentActivityResponse | TaskRecentActivityResponse422]:
    """Show what was worked on recently and by which models. Returns every task with activity in the last N
    hours (default 24) — tasks whose agent sessions ran during the window, tasks that completed in it,
    and tasks whose state changed in it — so both finished and still-in-progress work is included. Each
    item carries status, outcome, timestamps, and one entry per session attempt with the model that
    attempt actually reported ('models' is the distinct set, newest attempt first;
    'unattributed_attempts' counts attempts that reported no model, which is never guessed from the
    profile). The response also has 'by_model' totals across the window. Use this for 'what did we do
    yesterday' or 'which models did the work'.

     Show what was worked on recently and by which models. Returns every task with activity in the last N
    hours (default 24) — tasks whose agent sessions ran during the window, tasks that completed in it,
    and tasks whose state changed in it — so both finished and still-in-progress work is included. Each
    item carries status, outcome, timestamps, and one entry per session attempt with the model that
    attempt actually reported ('models' is the distinct set, newest attempt first;
    'unattributed_attempts' counts attempts that reported no model, which is never guessed from the
    profile). The response also has 'by_model' totals across the window. Use this for 'what did we do
    yesterday' or 'which models did the work'.

    Args:
        body (TaskRecentActivityRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskRecentActivityResponse | TaskRecentActivityResponse422]
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
    body: TaskRecentActivityRequest,
) -> TaskRecentActivityResponse | TaskRecentActivityResponse422 | None:
    """Show what was worked on recently and by which models. Returns every task with activity in the last N
    hours (default 24) — tasks whose agent sessions ran during the window, tasks that completed in it,
    and tasks whose state changed in it — so both finished and still-in-progress work is included. Each
    item carries status, outcome, timestamps, and one entry per session attempt with the model that
    attempt actually reported ('models' is the distinct set, newest attempt first;
    'unattributed_attempts' counts attempts that reported no model, which is never guessed from the
    profile). The response also has 'by_model' totals across the window. Use this for 'what did we do
    yesterday' or 'which models did the work'.

     Show what was worked on recently and by which models. Returns every task with activity in the last N
    hours (default 24) — tasks whose agent sessions ran during the window, tasks that completed in it,
    and tasks whose state changed in it — so both finished and still-in-progress work is included. Each
    item carries status, outcome, timestamps, and one entry per session attempt with the model that
    attempt actually reported ('models' is the distinct set, newest attempt first;
    'unattributed_attempts' counts attempts that reported no model, which is never guessed from the
    profile). The response also has 'by_model' totals across the window. Use this for 'what did we do
    yesterday' or 'which models did the work'.

    Args:
        body (TaskRecentActivityRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskRecentActivityResponse | TaskRecentActivityResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskRecentActivityRequest,
) -> Response[TaskRecentActivityResponse | TaskRecentActivityResponse422]:
    """Show what was worked on recently and by which models. Returns every task with activity in the last N
    hours (default 24) — tasks whose agent sessions ran during the window, tasks that completed in it,
    and tasks whose state changed in it — so both finished and still-in-progress work is included. Each
    item carries status, outcome, timestamps, and one entry per session attempt with the model that
    attempt actually reported ('models' is the distinct set, newest attempt first;
    'unattributed_attempts' counts attempts that reported no model, which is never guessed from the
    profile). The response also has 'by_model' totals across the window. Use this for 'what did we do
    yesterday' or 'which models did the work'.

     Show what was worked on recently and by which models. Returns every task with activity in the last N
    hours (default 24) — tasks whose agent sessions ran during the window, tasks that completed in it,
    and tasks whose state changed in it — so both finished and still-in-progress work is included. Each
    item carries status, outcome, timestamps, and one entry per session attempt with the model that
    attempt actually reported ('models' is the distinct set, newest attempt first;
    'unattributed_attempts' counts attempts that reported no model, which is never guessed from the
    profile). The response also has 'by_model' totals across the window. Use this for 'what did we do
    yesterday' or 'which models did the work'.

    Args:
        body (TaskRecentActivityRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskRecentActivityResponse | TaskRecentActivityResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: TaskRecentActivityRequest,
) -> TaskRecentActivityResponse | TaskRecentActivityResponse422 | None:
    """Show what was worked on recently and by which models. Returns every task with activity in the last N
    hours (default 24) — tasks whose agent sessions ran during the window, tasks that completed in it,
    and tasks whose state changed in it — so both finished and still-in-progress work is included. Each
    item carries status, outcome, timestamps, and one entry per session attempt with the model that
    attempt actually reported ('models' is the distinct set, newest attempt first;
    'unattributed_attempts' counts attempts that reported no model, which is never guessed from the
    profile). The response also has 'by_model' totals across the window. Use this for 'what did we do
    yesterday' or 'which models did the work'.

     Show what was worked on recently and by which models. Returns every task with activity in the last N
    hours (default 24) — tasks whose agent sessions ran during the window, tasks that completed in it,
    and tasks whose state changed in it — so both finished and still-in-progress work is included. Each
    item carries status, outcome, timestamps, and one entry per session attempt with the model that
    attempt actually reported ('models' is the distinct set, newest attempt first;
    'unattributed_attempts' counts attempts that reported no model, which is never guessed from the
    profile). The response also has 'by_model' totals across the window. Use this for 'what did we do
    yesterday' or 'which models did the work'.

    Args:
        body (TaskRecentActivityRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskRecentActivityResponse | TaskRecentActivityResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
