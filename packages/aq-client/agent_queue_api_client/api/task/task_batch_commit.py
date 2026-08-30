from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.task_batch_commit_request import TaskBatchCommitRequest
from ...models.task_batch_commit_response import TaskBatchCommitResponse
from ...models.task_batch_commit_response_422 import TaskBatchCommitResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: TaskBatchCommitRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/batch-commit",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> TaskBatchCommitResponse | TaskBatchCommitResponse422 | None:
    if response.status_code == 200:
        response_200 = TaskBatchCommitResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = TaskBatchCommitResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[TaskBatchCommitResponse | TaskBatchCommitResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskBatchCommitRequest,
) -> Response[TaskBatchCommitResponse | TaskBatchCommitResponse422]:
    """Atomically materialise a proposal into the live work graph: creates every task, then every
    dependency edge, stamping the proposal's source as provenance. The ready→committed flip is a single
    conditional update, so two concurrent commits cannot both win. Any failure unwinds every task and
    edge already created and returns the proposal to ``ready`` for a retry. Returns the created task
    ids.

     Atomically materialise a proposal into the live work graph: creates every task, then every
    dependency edge, stamping the proposal's source as provenance. The ready→committed flip is a single
    conditional update, so two concurrent commits cannot both win. Any failure unwinds every task and
    edge already created and returns the proposal to ``ready`` for a retry. Returns the created task
    ids.

    Args:
        body (TaskBatchCommitRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskBatchCommitResponse | TaskBatchCommitResponse422]
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
    body: TaskBatchCommitRequest,
) -> TaskBatchCommitResponse | TaskBatchCommitResponse422 | None:
    """Atomically materialise a proposal into the live work graph: creates every task, then every
    dependency edge, stamping the proposal's source as provenance. The ready→committed flip is a single
    conditional update, so two concurrent commits cannot both win. Any failure unwinds every task and
    edge already created and returns the proposal to ``ready`` for a retry. Returns the created task
    ids.

     Atomically materialise a proposal into the live work graph: creates every task, then every
    dependency edge, stamping the proposal's source as provenance. The ready→committed flip is a single
    conditional update, so two concurrent commits cannot both win. Any failure unwinds every task and
    edge already created and returns the proposal to ``ready`` for a retry. Returns the created task
    ids.

    Args:
        body (TaskBatchCommitRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskBatchCommitResponse | TaskBatchCommitResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskBatchCommitRequest,
) -> Response[TaskBatchCommitResponse | TaskBatchCommitResponse422]:
    """Atomically materialise a proposal into the live work graph: creates every task, then every
    dependency edge, stamping the proposal's source as provenance. The ready→committed flip is a single
    conditional update, so two concurrent commits cannot both win. Any failure unwinds every task and
    edge already created and returns the proposal to ``ready`` for a retry. Returns the created task
    ids.

     Atomically materialise a proposal into the live work graph: creates every task, then every
    dependency edge, stamping the proposal's source as provenance. The ready→committed flip is a single
    conditional update, so two concurrent commits cannot both win. Any failure unwinds every task and
    edge already created and returns the proposal to ``ready`` for a retry. Returns the created task
    ids.

    Args:
        body (TaskBatchCommitRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskBatchCommitResponse | TaskBatchCommitResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: TaskBatchCommitRequest,
) -> TaskBatchCommitResponse | TaskBatchCommitResponse422 | None:
    """Atomically materialise a proposal into the live work graph: creates every task, then every
    dependency edge, stamping the proposal's source as provenance. The ready→committed flip is a single
    conditional update, so two concurrent commits cannot both win. Any failure unwinds every task and
    edge already created and returns the proposal to ``ready`` for a retry. Returns the created task
    ids.

     Atomically materialise a proposal into the live work graph: creates every task, then every
    dependency edge, stamping the proposal's source as provenance. The ready→committed flip is a single
    conditional update, so two concurrent commits cannot both win. Any failure unwinds every task and
    edge already created and returns the proposal to ``ready`` for a retry. Returns the created task
    ids.

    Args:
        body (TaskBatchCommitRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskBatchCommitResponse | TaskBatchCommitResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
