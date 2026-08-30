from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.dry_run_playbook_request import DryRunPlaybookRequest
from ...models.dry_run_playbook_response import DryRunPlaybookResponse
from ...models.dry_run_playbook_response_422 import DryRunPlaybookResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: DryRunPlaybookRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/dry-run",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> DryRunPlaybookResponse | DryRunPlaybookResponse422 | None:
    if response.status_code == 200:
        response_200 = DryRunPlaybookResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = DryRunPlaybookResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[DryRunPlaybookResponse | DryRunPlaybookResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DryRunPlaybookRequest,
) -> Response[DryRunPlaybookResponse | DryRunPlaybookResponse422]:
    """Simulate playbook execution with a mock event, producing no side effects. Walks the graph from entry
    to terminal without real LLM calls, DB writes, or event emission. Returns the node trace showing the
    path that would be taken. Useful for testing and validating playbook design before deploying.

     Simulate playbook execution with a mock event, producing no side effects. Walks the graph from entry
    to terminal without real LLM calls, DB writes, or event emission. Returns the node trace showing the
    path that would be taken. Useful for testing and validating playbook design before deploying.

    Args:
        body (DryRunPlaybookRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DryRunPlaybookResponse | DryRunPlaybookResponse422]
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
    body: DryRunPlaybookRequest,
) -> DryRunPlaybookResponse | DryRunPlaybookResponse422 | None:
    """Simulate playbook execution with a mock event, producing no side effects. Walks the graph from entry
    to terminal without real LLM calls, DB writes, or event emission. Returns the node trace showing the
    path that would be taken. Useful for testing and validating playbook design before deploying.

     Simulate playbook execution with a mock event, producing no side effects. Walks the graph from entry
    to terminal without real LLM calls, DB writes, or event emission. Returns the node trace showing the
    path that would be taken. Useful for testing and validating playbook design before deploying.

    Args:
        body (DryRunPlaybookRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DryRunPlaybookResponse | DryRunPlaybookResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DryRunPlaybookRequest,
) -> Response[DryRunPlaybookResponse | DryRunPlaybookResponse422]:
    """Simulate playbook execution with a mock event, producing no side effects. Walks the graph from entry
    to terminal without real LLM calls, DB writes, or event emission. Returns the node trace showing the
    path that would be taken. Useful for testing and validating playbook design before deploying.

     Simulate playbook execution with a mock event, producing no side effects. Walks the graph from entry
    to terminal without real LLM calls, DB writes, or event emission. Returns the node trace showing the
    path that would be taken. Useful for testing and validating playbook design before deploying.

    Args:
        body (DryRunPlaybookRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DryRunPlaybookResponse | DryRunPlaybookResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: DryRunPlaybookRequest,
) -> DryRunPlaybookResponse | DryRunPlaybookResponse422 | None:
    """Simulate playbook execution with a mock event, producing no side effects. Walks the graph from entry
    to terminal without real LLM calls, DB writes, or event emission. Returns the node trace showing the
    path that would be taken. Useful for testing and validating playbook design before deploying.

     Simulate playbook execution with a mock event, producing no side effects. Walks the graph from entry
    to terminal without real LLM calls, DB writes, or event emission. Returns the node trace showing the
    path that would be taken. Useful for testing and validating playbook design before deploying.

    Args:
        body (DryRunPlaybookRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DryRunPlaybookResponse | DryRunPlaybookResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
