from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.task_subtask_update_request import TaskSubtaskUpdateRequest
from ...models.task_subtask_update_response import TaskSubtaskUpdateResponse
from ...models.task_subtask_update_response_422 import TaskSubtaskUpdateResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: TaskSubtaskUpdateRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/subtask-update",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> TaskSubtaskUpdateResponse | TaskSubtaskUpdateResponse422 | None:
    if response.status_code == 200:
        response_200 = TaskSubtaskUpdateResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = TaskSubtaskUpdateResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[TaskSubtaskUpdateResponse | TaskSubtaskUpdateResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskSubtaskUpdateRequest,
) -> Response[TaskSubtaskUpdateResponse | TaskSubtaskUpdateResponse422]:
    """Set a subtask's status and/or note. Backs 'aq task subtask-done/start/skip'.

     Set a subtask's status and/or note. Backs 'aq task subtask-done/start/skip'.

    Args:
        body (TaskSubtaskUpdateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskSubtaskUpdateResponse | TaskSubtaskUpdateResponse422]
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
    body: TaskSubtaskUpdateRequest,
) -> TaskSubtaskUpdateResponse | TaskSubtaskUpdateResponse422 | None:
    """Set a subtask's status and/or note. Backs 'aq task subtask-done/start/skip'.

     Set a subtask's status and/or note. Backs 'aq task subtask-done/start/skip'.

    Args:
        body (TaskSubtaskUpdateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskSubtaskUpdateResponse | TaskSubtaskUpdateResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskSubtaskUpdateRequest,
) -> Response[TaskSubtaskUpdateResponse | TaskSubtaskUpdateResponse422]:
    """Set a subtask's status and/or note. Backs 'aq task subtask-done/start/skip'.

     Set a subtask's status and/or note. Backs 'aq task subtask-done/start/skip'.

    Args:
        body (TaskSubtaskUpdateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskSubtaskUpdateResponse | TaskSubtaskUpdateResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: TaskSubtaskUpdateRequest,
) -> TaskSubtaskUpdateResponse | TaskSubtaskUpdateResponse422 | None:
    """Set a subtask's status and/or note. Backs 'aq task subtask-done/start/skip'.

     Set a subtask's status and/or note. Backs 'aq task subtask-done/start/skip'.

    Args:
        body (TaskSubtaskUpdateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskSubtaskUpdateResponse | TaskSubtaskUpdateResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
