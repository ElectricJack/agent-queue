from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.provider_held_tasks_request import ProviderHeldTasksRequest
from ...models.provider_held_tasks_response import ProviderHeldTasksResponse
from ...models.provider_held_tasks_response_422 import ProviderHeldTasksResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ProviderHeldTasksRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/provider/held-tasks",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ProviderHeldTasksResponse | ProviderHeldTasksResponse422 | None:
    if response.status_code == 200:
        response_200 = ProviderHeldTasksResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ProviderHeldTasksResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ProviderHeldTasksResponse | ProviderHeldTasksResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderHeldTasksRequest,
) -> Response[ProviderHeldTasksResponse | ProviderHeldTasksResponse422]:
    """List the queued tasks an unavailable provider is holding, each with why it is not moving
    (provider_pinned, no_equivalent_rung, awaiting_failover_capacity with how many are ahead, ...), the
    provider's state, since when and the expected recovery -- the same hold `aq task explain` reports.
    Empty while every provider is launchable.

     List the queued tasks an unavailable provider is holding, each with why it is not moving
    (provider_pinned, no_equivalent_rung, awaiting_failover_capacity with how many are ahead, ...), the
    provider's state, since when and the expected recovery -- the same hold `aq task explain` reports.
    Empty while every provider is launchable.

    Args:
        body (ProviderHeldTasksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProviderHeldTasksResponse | ProviderHeldTasksResponse422]
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
    body: ProviderHeldTasksRequest,
) -> ProviderHeldTasksResponse | ProviderHeldTasksResponse422 | None:
    """List the queued tasks an unavailable provider is holding, each with why it is not moving
    (provider_pinned, no_equivalent_rung, awaiting_failover_capacity with how many are ahead, ...), the
    provider's state, since when and the expected recovery -- the same hold `aq task explain` reports.
    Empty while every provider is launchable.

     List the queued tasks an unavailable provider is holding, each with why it is not moving
    (provider_pinned, no_equivalent_rung, awaiting_failover_capacity with how many are ahead, ...), the
    provider's state, since when and the expected recovery -- the same hold `aq task explain` reports.
    Empty while every provider is launchable.

    Args:
        body (ProviderHeldTasksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProviderHeldTasksResponse | ProviderHeldTasksResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderHeldTasksRequest,
) -> Response[ProviderHeldTasksResponse | ProviderHeldTasksResponse422]:
    """List the queued tasks an unavailable provider is holding, each with why it is not moving
    (provider_pinned, no_equivalent_rung, awaiting_failover_capacity with how many are ahead, ...), the
    provider's state, since when and the expected recovery -- the same hold `aq task explain` reports.
    Empty while every provider is launchable.

     List the queued tasks an unavailable provider is holding, each with why it is not moving
    (provider_pinned, no_equivalent_rung, awaiting_failover_capacity with how many are ahead, ...), the
    provider's state, since when and the expected recovery -- the same hold `aq task explain` reports.
    Empty while every provider is launchable.

    Args:
        body (ProviderHeldTasksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProviderHeldTasksResponse | ProviderHeldTasksResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderHeldTasksRequest,
) -> ProviderHeldTasksResponse | ProviderHeldTasksResponse422 | None:
    """List the queued tasks an unavailable provider is holding, each with why it is not moving
    (provider_pinned, no_equivalent_rung, awaiting_failover_capacity with how many are ahead, ...), the
    provider's state, since when and the expected recovery -- the same hold `aq task explain` reports.
    Empty while every provider is launchable.

     List the queued tasks an unavailable provider is holding, each with why it is not moving
    (provider_pinned, no_equivalent_rung, awaiting_failover_capacity with how many are ahead, ...), the
    provider's state, since when and the expected recovery -- the same hold `aq task explain` reports.
    Empty while every provider is launchable.

    Args:
        body (ProviderHeldTasksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProviderHeldTasksResponse | ProviderHeldTasksResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
