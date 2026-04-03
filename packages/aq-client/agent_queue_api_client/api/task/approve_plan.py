from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.approve_plan_request import ApprovePlanRequest
from ...models.approve_plan_response import ApprovePlanResponse
from ...models.approve_plan_response_422 import ApprovePlanResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ApprovePlanRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/approve-plan",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ApprovePlanResponse | ApprovePlanResponse422 | None:
    if response.status_code == 200:
        response_200 = ApprovePlanResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ApprovePlanResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ApprovePlanResponse | ApprovePlanResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ApprovePlanRequest,
) -> Response[ApprovePlanResponse | ApprovePlanResponse422]:
    """Approve a plan for a task in AWAITING_PLAN_APPROVAL status. Creates subtasks from the stored plan
    and marks the task completed.

     Approve a plan for a task in AWAITING_PLAN_APPROVAL status. Creates subtasks from the stored plan
    and marks the task completed.

    Args:
        body (ApprovePlanRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApprovePlanResponse | ApprovePlanResponse422]
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
    body: ApprovePlanRequest,
) -> ApprovePlanResponse | ApprovePlanResponse422 | None:
    """Approve a plan for a task in AWAITING_PLAN_APPROVAL status. Creates subtasks from the stored plan
    and marks the task completed.

     Approve a plan for a task in AWAITING_PLAN_APPROVAL status. Creates subtasks from the stored plan
    and marks the task completed.

    Args:
        body (ApprovePlanRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApprovePlanResponse | ApprovePlanResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ApprovePlanRequest,
) -> Response[ApprovePlanResponse | ApprovePlanResponse422]:
    """Approve a plan for a task in AWAITING_PLAN_APPROVAL status. Creates subtasks from the stored plan
    and marks the task completed.

     Approve a plan for a task in AWAITING_PLAN_APPROVAL status. Creates subtasks from the stored plan
    and marks the task completed.

    Args:
        body (ApprovePlanRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ApprovePlanResponse | ApprovePlanResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ApprovePlanRequest,
) -> ApprovePlanResponse | ApprovePlanResponse422 | None:
    """Approve a plan for a task in AWAITING_PLAN_APPROVAL status. Creates subtasks from the stored plan
    and marks the task completed.

     Approve a plan for a task in AWAITING_PLAN_APPROVAL status. Creates subtasks from the stored plan
    and marks the task completed.

    Args:
        body (ApprovePlanRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ApprovePlanResponse | ApprovePlanResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
