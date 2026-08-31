from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.resume_task_request import ResumeTaskRequest
from ...models.resume_task_response_422 import ResumeTaskResponse422
from ...models.task_control_response import TaskControlResponse
from ...types import Response


def _get_kwargs(
    *,
    body: ResumeTaskRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/resume",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ResumeTaskResponse422 | TaskControlResponse | None:
    if response.status_code == 200:
        response_200 = TaskControlResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ResumeTaskResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ResumeTaskResponse422 | TaskControlResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ResumeTaskRequest,
) -> Response[ResumeTaskResponse422 | TaskControlResponse]:
    """Resume a paused task, respecting its existing gates and approval state. Retries any unfinished
    session stop first.

     Resume a paused task, respecting its existing gates and approval state. Retries any unfinished
    session stop first.

    Args:
        body (ResumeTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ResumeTaskResponse422 | TaskControlResponse]
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
    body: ResumeTaskRequest,
) -> ResumeTaskResponse422 | TaskControlResponse | None:
    """Resume a paused task, respecting its existing gates and approval state. Retries any unfinished
    session stop first.

     Resume a paused task, respecting its existing gates and approval state. Retries any unfinished
    session stop first.

    Args:
        body (ResumeTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ResumeTaskResponse422 | TaskControlResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ResumeTaskRequest,
) -> Response[ResumeTaskResponse422 | TaskControlResponse]:
    """Resume a paused task, respecting its existing gates and approval state. Retries any unfinished
    session stop first.

     Resume a paused task, respecting its existing gates and approval state. Retries any unfinished
    session stop first.

    Args:
        body (ResumeTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ResumeTaskResponse422 | TaskControlResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ResumeTaskRequest,
) -> ResumeTaskResponse422 | TaskControlResponse | None:
    """Resume a paused task, respecting its existing gates and approval state. Retries any unfinished
    session stop first.

     Resume a paused task, respecting its existing gates and approval state. Retries any unfinished
    session stop first.

    Args:
        body (ResumeTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ResumeTaskResponse422 | TaskControlResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
