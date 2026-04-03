from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.list_projects_request import ListProjectsRequest
from ...models.list_projects_response import ListProjectsResponse
from ...models.list_projects_response_422 import ListProjectsResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ListProjectsRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/project/list",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ListProjectsResponse | ListProjectsResponse422 | None:
    if response.status_code == 200:
        response_200 = ListProjectsResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ListProjectsResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ListProjectsResponse | ListProjectsResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListProjectsRequest,
) -> Response[ListProjectsResponse | ListProjectsResponse422]:
    """List all projects in the system.

     List all projects in the system.

    Args:
        body (ListProjectsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListProjectsResponse | ListProjectsResponse422]
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
    body: ListProjectsRequest,
) -> ListProjectsResponse | ListProjectsResponse422 | None:
    """List all projects in the system.

     List all projects in the system.

    Args:
        body (ListProjectsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListProjectsResponse | ListProjectsResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListProjectsRequest,
) -> Response[ListProjectsResponse | ListProjectsResponse422]:
    """List all projects in the system.

     List all projects in the system.

    Args:
        body (ListProjectsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListProjectsResponse | ListProjectsResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ListProjectsRequest,
) -> ListProjectsResponse | ListProjectsResponse422 | None:
    """List all projects in the system.

     List all projects in the system.

    Args:
        body (ListProjectsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListProjectsResponse | ListProjectsResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
