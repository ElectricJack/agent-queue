from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.graph_layout_response import GraphLayoutResponse
from ...models.graph_tidy_request import GraphTidyRequest
from ...models.graph_tidy_response_422 import GraphTidyResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: GraphTidyRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/graph/tidy",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> GraphLayoutResponse | GraphTidyResponse422 | None:
    if response.status_code == 200:
        response_200 = GraphLayoutResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = GraphTidyResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[GraphLayoutResponse | GraphTidyResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GraphTidyRequest,
) -> Response[GraphLayoutResponse | GraphTidyResponse422]:
    """Enqueue a Tidy layout job for a project. Breaks spatial memory; user-triggered only.

     Enqueue a Tidy layout job for a project. Breaks spatial memory; user-triggered only.

    Args:
        body (GraphTidyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GraphLayoutResponse | GraphTidyResponse422]
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
    body: GraphTidyRequest,
) -> GraphLayoutResponse | GraphTidyResponse422 | None:
    """Enqueue a Tidy layout job for a project. Breaks spatial memory; user-triggered only.

     Enqueue a Tidy layout job for a project. Breaks spatial memory; user-triggered only.

    Args:
        body (GraphTidyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GraphLayoutResponse | GraphTidyResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GraphTidyRequest,
) -> Response[GraphLayoutResponse | GraphTidyResponse422]:
    """Enqueue a Tidy layout job for a project. Breaks spatial memory; user-triggered only.

     Enqueue a Tidy layout job for a project. Breaks spatial memory; user-triggered only.

    Args:
        body (GraphTidyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GraphLayoutResponse | GraphTidyResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: GraphTidyRequest,
) -> GraphLayoutResponse | GraphTidyResponse422 | None:
    """Enqueue a Tidy layout job for a project. Breaks spatial memory; user-triggered only.

     Enqueue a Tidy layout job for a project. Breaks spatial memory; user-triggered only.

    Args:
        body (GraphTidyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GraphLayoutResponse | GraphTidyResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
