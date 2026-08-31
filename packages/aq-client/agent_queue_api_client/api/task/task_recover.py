from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.task_recover_request import TaskRecoverRequest
from ...models.task_recover_response_422 import TaskRecoverResponse422
from ...models.task_recovery_response import TaskRecoveryResponse
from ...types import Response


def _get_kwargs(
    *,
    body: TaskRecoverRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/recover",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> TaskRecoverResponse422 | TaskRecoveryResponse | None:
    if response.status_code == 200:
        response_200 = TaskRecoveryResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = TaskRecoverResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[TaskRecoverResponse422 | TaskRecoveryResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskRecoverRequest,
) -> Response[TaskRecoverResponse422 | TaskRecoveryResponse]:
    """Decide a supervisor recovery incident: retry safely within existing budgets or hold with a recorded
    diagnosis. Never bypass a rejection using restart_task or status edits.

     Decide a supervisor recovery incident: retry safely within existing budgets or hold with a recorded
    diagnosis. Never bypass a rejection using restart_task or status edits.

    Args:
        body (TaskRecoverRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskRecoverResponse422 | TaskRecoveryResponse]
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
    body: TaskRecoverRequest,
) -> TaskRecoverResponse422 | TaskRecoveryResponse | None:
    """Decide a supervisor recovery incident: retry safely within existing budgets or hold with a recorded
    diagnosis. Never bypass a rejection using restart_task or status edits.

     Decide a supervisor recovery incident: retry safely within existing budgets or hold with a recorded
    diagnosis. Never bypass a rejection using restart_task or status edits.

    Args:
        body (TaskRecoverRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskRecoverResponse422 | TaskRecoveryResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskRecoverRequest,
) -> Response[TaskRecoverResponse422 | TaskRecoveryResponse]:
    """Decide a supervisor recovery incident: retry safely within existing budgets or hold with a recorded
    diagnosis. Never bypass a rejection using restart_task or status edits.

     Decide a supervisor recovery incident: retry safely within existing budgets or hold with a recorded
    diagnosis. Never bypass a rejection using restart_task or status edits.

    Args:
        body (TaskRecoverRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskRecoverResponse422 | TaskRecoveryResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: TaskRecoverRequest,
) -> TaskRecoverResponse422 | TaskRecoveryResponse | None:
    """Decide a supervisor recovery incident: retry safely within existing budgets or hold with a recorded
    diagnosis. Never bypass a rejection using restart_task or status edits.

     Decide a supervisor recovery incident: retry safely within existing budgets or hold with a recorded
    diagnosis. Never bypass a rejection using restart_task or status edits.

    Args:
        body (TaskRecoverRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskRecoverResponse422 | TaskRecoveryResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
