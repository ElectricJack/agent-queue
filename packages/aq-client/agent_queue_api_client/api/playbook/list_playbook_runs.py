from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.list_playbook_runs_request import ListPlaybookRunsRequest
from ...models.list_playbook_runs_response import ListPlaybookRunsResponse
from ...models.list_playbook_runs_response_422 import ListPlaybookRunsResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ListPlaybookRunsRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/list-runs",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ListPlaybookRunsResponse | ListPlaybookRunsResponse422 | None:
    if response.status_code == 200:
        response_200 = ListPlaybookRunsResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ListPlaybookRunsResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ListPlaybookRunsResponse | ListPlaybookRunsResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListPlaybookRunsRequest,
) -> Response[ListPlaybookRunsResponse | ListPlaybookRunsResponse422]:
    """List recent playbook runs with status and path taken through the graph. Each run includes a compact
    node trace showing visited nodes and their outcome. Filter by playbook_id and/or status (e.g.
    'paused' to find runs awaiting human review). Returns newest first.

     List recent playbook runs with status and path taken through the graph. Each run includes a compact
    node trace showing visited nodes and their outcome. Filter by playbook_id and/or status (e.g.
    'paused' to find runs awaiting human review). Returns newest first.

    Args:
        body (ListPlaybookRunsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListPlaybookRunsResponse | ListPlaybookRunsResponse422]
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
    body: ListPlaybookRunsRequest,
) -> ListPlaybookRunsResponse | ListPlaybookRunsResponse422 | None:
    """List recent playbook runs with status and path taken through the graph. Each run includes a compact
    node trace showing visited nodes and their outcome. Filter by playbook_id and/or status (e.g.
    'paused' to find runs awaiting human review). Returns newest first.

     List recent playbook runs with status and path taken through the graph. Each run includes a compact
    node trace showing visited nodes and their outcome. Filter by playbook_id and/or status (e.g.
    'paused' to find runs awaiting human review). Returns newest first.

    Args:
        body (ListPlaybookRunsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListPlaybookRunsResponse | ListPlaybookRunsResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListPlaybookRunsRequest,
) -> Response[ListPlaybookRunsResponse | ListPlaybookRunsResponse422]:
    """List recent playbook runs with status and path taken through the graph. Each run includes a compact
    node trace showing visited nodes and their outcome. Filter by playbook_id and/or status (e.g.
    'paused' to find runs awaiting human review). Returns newest first.

     List recent playbook runs with status and path taken through the graph. Each run includes a compact
    node trace showing visited nodes and their outcome. Filter by playbook_id and/or status (e.g.
    'paused' to find runs awaiting human review). Returns newest first.

    Args:
        body (ListPlaybookRunsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListPlaybookRunsResponse | ListPlaybookRunsResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ListPlaybookRunsRequest,
) -> ListPlaybookRunsResponse | ListPlaybookRunsResponse422 | None:
    """List recent playbook runs with status and path taken through the graph. Each run includes a compact
    node trace showing visited nodes and their outcome. Filter by playbook_id and/or status (e.g.
    'paused' to find runs awaiting human review). Returns newest first.

     List recent playbook runs with status and path taken through the graph. Each run includes a compact
    node trace showing visited nodes and their outcome. Filter by playbook_id and/or status (e.g.
    'paused' to find runs awaiting human review). Returns newest first.

    Args:
        body (ListPlaybookRunsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListPlaybookRunsResponse | ListPlaybookRunsResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
