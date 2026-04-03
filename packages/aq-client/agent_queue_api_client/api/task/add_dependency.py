from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.add_dependency_request import AddDependencyRequest
from ...models.add_dependency_response import AddDependencyResponse
from ...models.add_dependency_response_422 import AddDependencyResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: AddDependencyRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/add-dependency",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> AddDependencyResponse | AddDependencyResponse422 | None:
    if response.status_code == 200:
        response_200 = AddDependencyResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = AddDependencyResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[AddDependencyResponse | AddDependencyResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: AddDependencyRequest,
) -> Response[AddDependencyResponse | AddDependencyResponse422]:
    """Add a dependency between two tasks: task_id will depend on depends_on (i.e. task_id cannot start
    until depends_on is completed). Validates that both tasks exist and performs cycle detection to
    prevent circular dependency chains. Use this when the user wants to link tasks so one must finish
    before another can begin.

     Add a dependency between two tasks: task_id will depend on depends_on (i.e. task_id cannot start
    until depends_on is completed). Validates that both tasks exist and performs cycle detection to
    prevent circular dependency chains. Use this when the user wants to link tasks so one must finish
    before another can begin.

    Args:
        body (AddDependencyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[AddDependencyResponse | AddDependencyResponse422]
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
    body: AddDependencyRequest,
) -> AddDependencyResponse | AddDependencyResponse422 | None:
    """Add a dependency between two tasks: task_id will depend on depends_on (i.e. task_id cannot start
    until depends_on is completed). Validates that both tasks exist and performs cycle detection to
    prevent circular dependency chains. Use this when the user wants to link tasks so one must finish
    before another can begin.

     Add a dependency between two tasks: task_id will depend on depends_on (i.e. task_id cannot start
    until depends_on is completed). Validates that both tasks exist and performs cycle detection to
    prevent circular dependency chains. Use this when the user wants to link tasks so one must finish
    before another can begin.

    Args:
        body (AddDependencyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        AddDependencyResponse | AddDependencyResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: AddDependencyRequest,
) -> Response[AddDependencyResponse | AddDependencyResponse422]:
    """Add a dependency between two tasks: task_id will depend on depends_on (i.e. task_id cannot start
    until depends_on is completed). Validates that both tasks exist and performs cycle detection to
    prevent circular dependency chains. Use this when the user wants to link tasks so one must finish
    before another can begin.

     Add a dependency between two tasks: task_id will depend on depends_on (i.e. task_id cannot start
    until depends_on is completed). Validates that both tasks exist and performs cycle detection to
    prevent circular dependency chains. Use this when the user wants to link tasks so one must finish
    before another can begin.

    Args:
        body (AddDependencyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[AddDependencyResponse | AddDependencyResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: AddDependencyRequest,
) -> AddDependencyResponse | AddDependencyResponse422 | None:
    """Add a dependency between two tasks: task_id will depend on depends_on (i.e. task_id cannot start
    until depends_on is completed). Validates that both tasks exist and performs cycle detection to
    prevent circular dependency chains. Use this when the user wants to link tasks so one must finish
    before another can begin.

     Add a dependency between two tasks: task_id will depend on depends_on (i.e. task_id cannot start
    until depends_on is completed). Validates that both tasks exist and performs cycle detection to
    prevent circular dependency chains. Use this when the user wants to link tasks so one must finish
    before another can begin.

    Args:
        body (AddDependencyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        AddDependencyResponse | AddDependencyResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
