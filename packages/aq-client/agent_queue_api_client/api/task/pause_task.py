from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.pause_task_request import PauseTaskRequest
from ...models.pause_task_response_422 import PauseTaskResponse422
from ...models.task_control_response import TaskControlResponse
from ...types import Response


def _get_kwargs(
    *,
    body: PauseTaskRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/pause",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PauseTaskResponse422 | TaskControlResponse | None:
    if response.status_code == 200:
        response_200 = TaskControlResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PauseTaskResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PauseTaskResponse422 | TaskControlResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PauseTaskRequest,
) -> Response[PauseTaskResponse422 | TaskControlResponse]:
    """Pause a queued or running task until explicit Resume. Preserves work, routing, retries, and approval
    state.

     Pause a queued or running task until explicit Resume. Preserves work, routing, retries, and approval
    state.

    Args:
        body (PauseTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PauseTaskResponse422 | TaskControlResponse]
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
    body: PauseTaskRequest,
) -> PauseTaskResponse422 | TaskControlResponse | None:
    """Pause a queued or running task until explicit Resume. Preserves work, routing, retries, and approval
    state.

     Pause a queued or running task until explicit Resume. Preserves work, routing, retries, and approval
    state.

    Args:
        body (PauseTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PauseTaskResponse422 | TaskControlResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PauseTaskRequest,
) -> Response[PauseTaskResponse422 | TaskControlResponse]:
    """Pause a queued or running task until explicit Resume. Preserves work, routing, retries, and approval
    state.

     Pause a queued or running task until explicit Resume. Preserves work, routing, retries, and approval
    state.

    Args:
        body (PauseTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PauseTaskResponse422 | TaskControlResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PauseTaskRequest,
) -> PauseTaskResponse422 | TaskControlResponse | None:
    """Pause a queued or running task until explicit Resume. Preserves work, routing, retries, and approval
    state.

     Pause a queued or running task until explicit Resume. Preserves work, routing, retries, and approval
    state.

    Args:
        body (PauseTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PauseTaskResponse422 | TaskControlResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
