from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.task_batch_propose_request import TaskBatchProposeRequest
from ...models.task_batch_propose_response import TaskBatchProposeResponse
from ...models.task_batch_propose_response_422 import TaskBatchProposeResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: TaskBatchProposeRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/batch-propose",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> TaskBatchProposeResponse | TaskBatchProposeResponse422 | None:
    if response.status_code == 200:
        response_200 = TaskBatchProposeResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = TaskBatchProposeResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[TaskBatchProposeResponse | TaskBatchProposeResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskBatchProposeRequest,
) -> Response[TaskBatchProposeResponse | TaskBatchProposeResponse422]:
    """Propose a batch of tasks and their dependency edges as one reviewable graph, without creating
    anything live. Tasks are identified by caller-chosen ``tempId``s that edges reference; edges may
    also point at existing task ids. The proposal is rejected up front if the shape is wrong, if it
    references tasks that do not exist, or if it would introduce a dependency cycle against the
    project's current graph. Returns a proposal_id for task_batch_update / _commit / _discard.

     Propose a batch of tasks and their dependency edges as one reviewable graph, without creating
    anything live. Tasks are identified by caller-chosen ``tempId``s that edges reference; edges may
    also point at existing task ids. The proposal is rejected up front if the shape is wrong, if it
    references tasks that do not exist, or if it would introduce a dependency cycle against the
    project's current graph. Returns a proposal_id for task_batch_update / _commit / _discard.

    Args:
        body (TaskBatchProposeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskBatchProposeResponse | TaskBatchProposeResponse422]
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
    body: TaskBatchProposeRequest,
) -> TaskBatchProposeResponse | TaskBatchProposeResponse422 | None:
    """Propose a batch of tasks and their dependency edges as one reviewable graph, without creating
    anything live. Tasks are identified by caller-chosen ``tempId``s that edges reference; edges may
    also point at existing task ids. The proposal is rejected up front if the shape is wrong, if it
    references tasks that do not exist, or if it would introduce a dependency cycle against the
    project's current graph. Returns a proposal_id for task_batch_update / _commit / _discard.

     Propose a batch of tasks and their dependency edges as one reviewable graph, without creating
    anything live. Tasks are identified by caller-chosen ``tempId``s that edges reference; edges may
    also point at existing task ids. The proposal is rejected up front if the shape is wrong, if it
    references tasks that do not exist, or if it would introduce a dependency cycle against the
    project's current graph. Returns a proposal_id for task_batch_update / _commit / _discard.

    Args:
        body (TaskBatchProposeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskBatchProposeResponse | TaskBatchProposeResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskBatchProposeRequest,
) -> Response[TaskBatchProposeResponse | TaskBatchProposeResponse422]:
    """Propose a batch of tasks and their dependency edges as one reviewable graph, without creating
    anything live. Tasks are identified by caller-chosen ``tempId``s that edges reference; edges may
    also point at existing task ids. The proposal is rejected up front if the shape is wrong, if it
    references tasks that do not exist, or if it would introduce a dependency cycle against the
    project's current graph. Returns a proposal_id for task_batch_update / _commit / _discard.

     Propose a batch of tasks and their dependency edges as one reviewable graph, without creating
    anything live. Tasks are identified by caller-chosen ``tempId``s that edges reference; edges may
    also point at existing task ids. The proposal is rejected up front if the shape is wrong, if it
    references tasks that do not exist, or if it would introduce a dependency cycle against the
    project's current graph. Returns a proposal_id for task_batch_update / _commit / _discard.

    Args:
        body (TaskBatchProposeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskBatchProposeResponse | TaskBatchProposeResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: TaskBatchProposeRequest,
) -> TaskBatchProposeResponse | TaskBatchProposeResponse422 | None:
    """Propose a batch of tasks and their dependency edges as one reviewable graph, without creating
    anything live. Tasks are identified by caller-chosen ``tempId``s that edges reference; edges may
    also point at existing task ids. The proposal is rejected up front if the shape is wrong, if it
    references tasks that do not exist, or if it would introduce a dependency cycle against the
    project's current graph. Returns a proposal_id for task_batch_update / _commit / _discard.

     Propose a batch of tasks and their dependency edges as one reviewable graph, without creating
    anything live. Tasks are identified by caller-chosen ``tempId``s that edges reference; edges may
    also point at existing task ids. The proposal is rejected up front if the shape is wrong, if it
    references tasks that do not exist, or if it would introduce a dependency cycle against the
    project's current graph. Returns a proposal_id for task_batch_update / _commit / _discard.

    Args:
        body (TaskBatchProposeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskBatchProposeResponse | TaskBatchProposeResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
