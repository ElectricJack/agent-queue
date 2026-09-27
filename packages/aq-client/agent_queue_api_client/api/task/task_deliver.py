from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.task_deliver_request import TaskDeliverRequest
from ...models.task_deliver_response import TaskDeliverResponse
from ...models.task_deliver_response_422 import TaskDeliverResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: TaskDeliverRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/deliver",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> TaskDeliverResponse | TaskDeliverResponse422 | None:
    if response.status_code == 200:
        response_200 = TaskDeliverResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = TaskDeliverResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[TaskDeliverResponse | TaskDeliverResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskDeliverRequest,
) -> Response[TaskDeliverResponse | TaskDeliverResponse422]:
    """Deliver a BLOCKED task's pushed branch into its repository's default branch by hand, then complete
    the task.  For work whose worker closed pass and pushed but whose close stopped at delivery, e.g. a
    project that pushes to a bare repository on disk, where no pull request can exist.  Merges (or fast-
    forwards) in a private repository, never in a worker slot or operator checkout, and pushes with a
    lease on the default branch as fetched.  Refuses a task that is not BLOCKED, whose last close was
    not a pass, has open children, belongs to a development/hierarchy/train project, integrates by pull
    request on a repository that can host one (merge the PR instead), was never pushed, or conflicts.
    Local operator or the project's live supervisor only.

     Deliver a BLOCKED task's pushed branch into its repository's default branch by hand, then complete
    the task.  For work whose worker closed pass and pushed but whose close stopped at delivery, e.g. a
    project that pushes to a bare repository on disk, where no pull request can exist.  Merges (or fast-
    forwards) in a private repository, never in a worker slot or operator checkout, and pushes with a
    lease on the default branch as fetched.  Refuses a task that is not BLOCKED, whose last close was
    not a pass, has open children, belongs to a development/hierarchy/train project, integrates by pull
    request on a repository that can host one (merge the PR instead), was never pushed, or conflicts.
    Local operator or the project's live supervisor only.

    Args:
        body (TaskDeliverRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskDeliverResponse | TaskDeliverResponse422]
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
    body: TaskDeliverRequest,
) -> TaskDeliverResponse | TaskDeliverResponse422 | None:
    """Deliver a BLOCKED task's pushed branch into its repository's default branch by hand, then complete
    the task.  For work whose worker closed pass and pushed but whose close stopped at delivery, e.g. a
    project that pushes to a bare repository on disk, where no pull request can exist.  Merges (or fast-
    forwards) in a private repository, never in a worker slot or operator checkout, and pushes with a
    lease on the default branch as fetched.  Refuses a task that is not BLOCKED, whose last close was
    not a pass, has open children, belongs to a development/hierarchy/train project, integrates by pull
    request on a repository that can host one (merge the PR instead), was never pushed, or conflicts.
    Local operator or the project's live supervisor only.

     Deliver a BLOCKED task's pushed branch into its repository's default branch by hand, then complete
    the task.  For work whose worker closed pass and pushed but whose close stopped at delivery, e.g. a
    project that pushes to a bare repository on disk, where no pull request can exist.  Merges (or fast-
    forwards) in a private repository, never in a worker slot or operator checkout, and pushes with a
    lease on the default branch as fetched.  Refuses a task that is not BLOCKED, whose last close was
    not a pass, has open children, belongs to a development/hierarchy/train project, integrates by pull
    request on a repository that can host one (merge the PR instead), was never pushed, or conflicts.
    Local operator or the project's live supervisor only.

    Args:
        body (TaskDeliverRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskDeliverResponse | TaskDeliverResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskDeliverRequest,
) -> Response[TaskDeliverResponse | TaskDeliverResponse422]:
    """Deliver a BLOCKED task's pushed branch into its repository's default branch by hand, then complete
    the task.  For work whose worker closed pass and pushed but whose close stopped at delivery, e.g. a
    project that pushes to a bare repository on disk, where no pull request can exist.  Merges (or fast-
    forwards) in a private repository, never in a worker slot or operator checkout, and pushes with a
    lease on the default branch as fetched.  Refuses a task that is not BLOCKED, whose last close was
    not a pass, has open children, belongs to a development/hierarchy/train project, integrates by pull
    request on a repository that can host one (merge the PR instead), was never pushed, or conflicts.
    Local operator or the project's live supervisor only.

     Deliver a BLOCKED task's pushed branch into its repository's default branch by hand, then complete
    the task.  For work whose worker closed pass and pushed but whose close stopped at delivery, e.g. a
    project that pushes to a bare repository on disk, where no pull request can exist.  Merges (or fast-
    forwards) in a private repository, never in a worker slot or operator checkout, and pushes with a
    lease on the default branch as fetched.  Refuses a task that is not BLOCKED, whose last close was
    not a pass, has open children, belongs to a development/hierarchy/train project, integrates by pull
    request on a repository that can host one (merge the PR instead), was never pushed, or conflicts.
    Local operator or the project's live supervisor only.

    Args:
        body (TaskDeliverRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskDeliverResponse | TaskDeliverResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: TaskDeliverRequest,
) -> TaskDeliverResponse | TaskDeliverResponse422 | None:
    """Deliver a BLOCKED task's pushed branch into its repository's default branch by hand, then complete
    the task.  For work whose worker closed pass and pushed but whose close stopped at delivery, e.g. a
    project that pushes to a bare repository on disk, where no pull request can exist.  Merges (or fast-
    forwards) in a private repository, never in a worker slot or operator checkout, and pushes with a
    lease on the default branch as fetched.  Refuses a task that is not BLOCKED, whose last close was
    not a pass, has open children, belongs to a development/hierarchy/train project, integrates by pull
    request on a repository that can host one (merge the PR instead), was never pushed, or conflicts.
    Local operator or the project's live supervisor only.

     Deliver a BLOCKED task's pushed branch into its repository's default branch by hand, then complete
    the task.  For work whose worker closed pass and pushed but whose close stopped at delivery, e.g. a
    project that pushes to a bare repository on disk, where no pull request can exist.  Merges (or fast-
    forwards) in a private repository, never in a worker slot or operator checkout, and pushes with a
    lease on the default branch as fetched.  Refuses a task that is not BLOCKED, whose last close was
    not a pass, has open children, belongs to a development/hierarchy/train project, integrates by pull
    request on a repository that can host one (merge the PR instead), was never pushed, or conflicts.
    Local operator or the project's live supervisor only.

    Args:
        body (TaskDeliverRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskDeliverResponse | TaskDeliverResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
