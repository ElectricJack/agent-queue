from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.phase_create_request import PhaseCreateRequest
from ...models.phase_create_response import PhaseCreateResponse
from ...models.phase_create_response_422 import PhaseCreateResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PhaseCreateRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/phase-create",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PhaseCreateResponse | PhaseCreateResponse422 | None:
    if response.status_code == 200:
        response_200 = PhaseCreateResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PhaseCreateResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PhaseCreateResponse | PhaseCreateResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PhaseCreateRequest,
) -> Response[PhaseCreateResponse | PhaseCreateResponse422]:
    """Create the next ordered phase in a project (or under an epic). A phase is a container task:
    everything filed under phase N+1 waits until every child of phase N has completed, with no per-task
    dependency edges. Work joins a phase with 'aq task create --parent <phase-id>'.

     Create the next ordered phase in a project (or under an epic). A phase is a container task:
    everything filed under phase N+1 waits until every child of phase N has completed, with no per-task
    dependency edges. Work joins a phase with 'aq task create --parent <phase-id>'.

    Args:
        body (PhaseCreateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PhaseCreateResponse | PhaseCreateResponse422]
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
    body: PhaseCreateRequest,
) -> PhaseCreateResponse | PhaseCreateResponse422 | None:
    """Create the next ordered phase in a project (or under an epic). A phase is a container task:
    everything filed under phase N+1 waits until every child of phase N has completed, with no per-task
    dependency edges. Work joins a phase with 'aq task create --parent <phase-id>'.

     Create the next ordered phase in a project (or under an epic). A phase is a container task:
    everything filed under phase N+1 waits until every child of phase N has completed, with no per-task
    dependency edges. Work joins a phase with 'aq task create --parent <phase-id>'.

    Args:
        body (PhaseCreateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PhaseCreateResponse | PhaseCreateResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PhaseCreateRequest,
) -> Response[PhaseCreateResponse | PhaseCreateResponse422]:
    """Create the next ordered phase in a project (or under an epic). A phase is a container task:
    everything filed under phase N+1 waits until every child of phase N has completed, with no per-task
    dependency edges. Work joins a phase with 'aq task create --parent <phase-id>'.

     Create the next ordered phase in a project (or under an epic). A phase is a container task:
    everything filed under phase N+1 waits until every child of phase N has completed, with no per-task
    dependency edges. Work joins a phase with 'aq task create --parent <phase-id>'.

    Args:
        body (PhaseCreateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PhaseCreateResponse | PhaseCreateResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PhaseCreateRequest,
) -> PhaseCreateResponse | PhaseCreateResponse422 | None:
    """Create the next ordered phase in a project (or under an epic). A phase is a container task:
    everything filed under phase N+1 waits until every child of phase N has completed, with no per-task
    dependency edges. Work joins a phase with 'aq task create --parent <phase-id>'.

     Create the next ordered phase in a project (or under an epic). A phase is a container task:
    everything filed under phase N+1 waits until every child of phase N has completed, with no per-task
    dependency edges. Work joins a phase with 'aq task create --parent <phase-id>'.

    Args:
        body (PhaseCreateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PhaseCreateResponse | PhaseCreateResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
