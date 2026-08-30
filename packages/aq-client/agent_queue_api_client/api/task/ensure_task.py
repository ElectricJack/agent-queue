from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.ensure_task_request import EnsureTaskRequest
from ...models.ensure_task_response import EnsureTaskResponse
from ...models.ensure_task_response_422 import EnsureTaskResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: EnsureTaskRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/ensure",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> EnsureTaskResponse | EnsureTaskResponse422 | None:
    if response.status_code == 200:
        response_200 = EnsureTaskResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = EnsureTaskResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[EnsureTaskResponse | EnsureTaskResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: EnsureTaskRequest,
) -> Response[EnsureTaskResponse | EnsureTaskResponse422]:
    """Find-or-create a task by (project_id, dedup_key). If an open task (not COMPLETED/FAILED) with the
    same dedup_key exists in the project, it is returned instead of creating a duplicate. Used by
    pipeline playbooks to coalesce recurring control-plane work (e.g. one open triage task per project).
    Returns {task_id, created}.

     Find-or-create a task by (project_id, dedup_key). If an open task (not COMPLETED/FAILED) with the
    same dedup_key exists in the project, it is returned instead of creating a duplicate. Used by
    pipeline playbooks to coalesce recurring control-plane work (e.g. one open triage task per project).
    Returns {task_id, created}.

    Args:
        body (EnsureTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EnsureTaskResponse | EnsureTaskResponse422]
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
    body: EnsureTaskRequest,
) -> EnsureTaskResponse | EnsureTaskResponse422 | None:
    """Find-or-create a task by (project_id, dedup_key). If an open task (not COMPLETED/FAILED) with the
    same dedup_key exists in the project, it is returned instead of creating a duplicate. Used by
    pipeline playbooks to coalesce recurring control-plane work (e.g. one open triage task per project).
    Returns {task_id, created}.

     Find-or-create a task by (project_id, dedup_key). If an open task (not COMPLETED/FAILED) with the
    same dedup_key exists in the project, it is returned instead of creating a duplicate. Used by
    pipeline playbooks to coalesce recurring control-plane work (e.g. one open triage task per project).
    Returns {task_id, created}.

    Args:
        body (EnsureTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EnsureTaskResponse | EnsureTaskResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: EnsureTaskRequest,
) -> Response[EnsureTaskResponse | EnsureTaskResponse422]:
    """Find-or-create a task by (project_id, dedup_key). If an open task (not COMPLETED/FAILED) with the
    same dedup_key exists in the project, it is returned instead of creating a duplicate. Used by
    pipeline playbooks to coalesce recurring control-plane work (e.g. one open triage task per project).
    Returns {task_id, created}.

     Find-or-create a task by (project_id, dedup_key). If an open task (not COMPLETED/FAILED) with the
    same dedup_key exists in the project, it is returned instead of creating a duplicate. Used by
    pipeline playbooks to coalesce recurring control-plane work (e.g. one open triage task per project).
    Returns {task_id, created}.

    Args:
        body (EnsureTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EnsureTaskResponse | EnsureTaskResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: EnsureTaskRequest,
) -> EnsureTaskResponse | EnsureTaskResponse422 | None:
    """Find-or-create a task by (project_id, dedup_key). If an open task (not COMPLETED/FAILED) with the
    same dedup_key exists in the project, it is returned instead of creating a duplicate. Used by
    pipeline playbooks to coalesce recurring control-plane work (e.g. one open triage task per project).
    Returns {task_id, created}.

     Find-or-create a task by (project_id, dedup_key). If an open task (not COMPLETED/FAILED) with the
    same dedup_key exists in the project, it is returned instead of creating a duplicate. Used by
    pipeline playbooks to coalesce recurring control-plane work (e.g. one open triage task per project).
    Returns {task_id, created}.

    Args:
        body (EnsureTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EnsureTaskResponse | EnsureTaskResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
