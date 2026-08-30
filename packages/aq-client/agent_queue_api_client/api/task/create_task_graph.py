from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.create_task_graph_request import CreateTaskGraphRequest
from ...models.create_task_graph_response_422 import CreateTaskGraphResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: CreateTaskGraphRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/create-graph",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | CreateTaskGraphResponse422 | None:
    if response.status_code == 200:
        response_200 = response.json()
        return response_200

    if response.status_code == 422:
        response_422 = CreateTaskGraphResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Any | CreateTaskGraphResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CreateTaskGraphRequest,
) -> Response[Any | CreateTaskGraphResponse422]:
    """Create a whole task graph in one transaction from a graph document or a vault spec's fenced aq-graph
    block.  Validates vars, keys, dependency types, profiles, cycles, and spec references first; dry_run
    returns the report without writing.

     Create a whole task graph in one transaction from a graph document or a vault spec's fenced aq-graph
    block.  Validates vars, keys, dependency types, profiles, cycles, and spec references first; dry_run
    returns the report without writing.

    Args:
        body (CreateTaskGraphRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | CreateTaskGraphResponse422]
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
    body: CreateTaskGraphRequest,
) -> Any | CreateTaskGraphResponse422 | None:
    """Create a whole task graph in one transaction from a graph document or a vault spec's fenced aq-graph
    block.  Validates vars, keys, dependency types, profiles, cycles, and spec references first; dry_run
    returns the report without writing.

     Create a whole task graph in one transaction from a graph document or a vault spec's fenced aq-graph
    block.  Validates vars, keys, dependency types, profiles, cycles, and spec references first; dry_run
    returns the report without writing.

    Args:
        body (CreateTaskGraphRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | CreateTaskGraphResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CreateTaskGraphRequest,
) -> Response[Any | CreateTaskGraphResponse422]:
    """Create a whole task graph in one transaction from a graph document or a vault spec's fenced aq-graph
    block.  Validates vars, keys, dependency types, profiles, cycles, and spec references first; dry_run
    returns the report without writing.

     Create a whole task graph in one transaction from a graph document or a vault spec's fenced aq-graph
    block.  Validates vars, keys, dependency types, profiles, cycles, and spec references first; dry_run
    returns the report without writing.

    Args:
        body (CreateTaskGraphRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | CreateTaskGraphResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: CreateTaskGraphRequest,
) -> Any | CreateTaskGraphResponse422 | None:
    """Create a whole task graph in one transaction from a graph document or a vault spec's fenced aq-graph
    block.  Validates vars, keys, dependency types, profiles, cycles, and spec references first; dry_run
    returns the report without writing.

     Create a whole task graph in one transaction from a graph document or a vault spec's fenced aq-graph
    block.  Validates vars, keys, dependency types, profiles, cycles, and spec references first; dry_run
    returns the report without writing.

    Args:
        body (CreateTaskGraphRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | CreateTaskGraphResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
