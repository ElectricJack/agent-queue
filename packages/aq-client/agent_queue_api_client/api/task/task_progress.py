from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.task_progress_request import TaskProgressRequest
from ...models.task_progress_response import TaskProgressResponse
from ...models.task_progress_response_422 import TaskProgressResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: TaskProgressRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/progress",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> TaskProgressResponse | TaskProgressResponse422 | None:
    if response.status_code == 200:
        response_200 = TaskProgressResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = TaskProgressResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[TaskProgressResponse | TaskProgressResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskProgressRequest,
) -> Response[TaskProgressResponse | TaskProgressResponse422]:
    """Computed progress for a container: counts, Kahn waves, max parallelism.

     Computed progress for a container: counts, Kahn waves, max parallelism.

    Args:
        body (TaskProgressRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskProgressResponse | TaskProgressResponse422]
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
    body: TaskProgressRequest,
) -> TaskProgressResponse | TaskProgressResponse422 | None:
    """Computed progress for a container: counts, Kahn waves, max parallelism.

     Computed progress for a container: counts, Kahn waves, max parallelism.

    Args:
        body (TaskProgressRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskProgressResponse | TaskProgressResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskProgressRequest,
) -> Response[TaskProgressResponse | TaskProgressResponse422]:
    """Computed progress for a container: counts, Kahn waves, max parallelism.

     Computed progress for a container: counts, Kahn waves, max parallelism.

    Args:
        body (TaskProgressRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskProgressResponse | TaskProgressResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: TaskProgressRequest,
) -> TaskProgressResponse | TaskProgressResponse422 | None:
    """Computed progress for a container: counts, Kahn waves, max parallelism.

     Computed progress for a container: counts, Kahn waves, max parallelism.

    Args:
        body (TaskProgressRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskProgressResponse | TaskProgressResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
