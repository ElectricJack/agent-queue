from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.process_plan_request import ProcessPlanRequest
from ...models.process_plan_response import ProcessPlanResponse
from ...models.process_plan_response_422 import ProcessPlanResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ProcessPlanRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/process-plan",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ProcessPlanResponse | ProcessPlanResponse422 | None:
    if response.status_code == 200:
        response_200 = ProcessPlanResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ProcessPlanResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ProcessPlanResponse | ProcessPlanResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProcessPlanRequest,
) -> Response[ProcessPlanResponse | ProcessPlanResponse422]:
    """Manually scan project workspaces for plan.md files and present them for approval. Use when the
    supervisor missed auto-detection or a plan was dropped into a workspace manually.

     Manually scan project workspaces for plan.md files and present them for approval. Use when the
    supervisor missed auto-detection or a plan was dropped into a workspace manually.

    Args:
        body (ProcessPlanRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProcessPlanResponse | ProcessPlanResponse422]
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
    body: ProcessPlanRequest,
) -> ProcessPlanResponse | ProcessPlanResponse422 | None:
    """Manually scan project workspaces for plan.md files and present them for approval. Use when the
    supervisor missed auto-detection or a plan was dropped into a workspace manually.

     Manually scan project workspaces for plan.md files and present them for approval. Use when the
    supervisor missed auto-detection or a plan was dropped into a workspace manually.

    Args:
        body (ProcessPlanRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProcessPlanResponse | ProcessPlanResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProcessPlanRequest,
) -> Response[ProcessPlanResponse | ProcessPlanResponse422]:
    """Manually scan project workspaces for plan.md files and present them for approval. Use when the
    supervisor missed auto-detection or a plan was dropped into a workspace manually.

     Manually scan project workspaces for plan.md files and present them for approval. Use when the
    supervisor missed auto-detection or a plan was dropped into a workspace manually.

    Args:
        body (ProcessPlanRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProcessPlanResponse | ProcessPlanResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ProcessPlanRequest,
) -> ProcessPlanResponse | ProcessPlanResponse422 | None:
    """Manually scan project workspaces for plan.md files and present them for approval. Use when the
    supervisor missed auto-detection or a plan was dropped into a workspace manually.

     Manually scan project workspaces for plan.md files and present them for approval. Use when the
    supervisor missed auto-detection or a plan was dropped into a workspace manually.

    Args:
        body (ProcessPlanRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProcessPlanResponse | ProcessPlanResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
