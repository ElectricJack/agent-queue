from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.provider_set_state_request import ProviderSetStateRequest
from ...models.provider_set_state_response import ProviderSetStateResponse
from ...models.provider_set_state_response_422 import ProviderSetStateResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ProviderSetStateRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/provider/set-state",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ProviderSetStateResponse | ProviderSetStateResponse422 | None:
    if response.status_code == 200:
        response_200 = ProviderSetStateResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ProviderSetStateResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ProviderSetStateResponse | ProviderSetStateResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderSetStateRequest,
) -> Response[ProviderSetStateResponse | ProviderSetStateResponse422]:
    """Override a provider's availability.  'disabled' stops every launch against it; 'available' forces it
    launchable against the evidence and always expires; 'auto' clears the override, resets the failure
    counters and re-derives the state from evidence.  An override lasts --for a duration (90s, 30m, 4h,
    2d) or --until a timestamp, else provider_failover.override.default_ttl_seconds, and never longer
    than override.max_ttl_seconds; --no-expiry is accepted for 'disabled' only.  Operators and
    supervisors only.

     Override a provider's availability.  'disabled' stops every launch against it; 'available' forces it
    launchable against the evidence and always expires; 'auto' clears the override, resets the failure
    counters and re-derives the state from evidence.  An override lasts --for a duration (90s, 30m, 4h,
    2d) or --until a timestamp, else provider_failover.override.default_ttl_seconds, and never longer
    than override.max_ttl_seconds; --no-expiry is accepted for 'disabled' only.  Operators and
    supervisors only.

    Args:
        body (ProviderSetStateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProviderSetStateResponse | ProviderSetStateResponse422]
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
    body: ProviderSetStateRequest,
) -> ProviderSetStateResponse | ProviderSetStateResponse422 | None:
    """Override a provider's availability.  'disabled' stops every launch against it; 'available' forces it
    launchable against the evidence and always expires; 'auto' clears the override, resets the failure
    counters and re-derives the state from evidence.  An override lasts --for a duration (90s, 30m, 4h,
    2d) or --until a timestamp, else provider_failover.override.default_ttl_seconds, and never longer
    than override.max_ttl_seconds; --no-expiry is accepted for 'disabled' only.  Operators and
    supervisors only.

     Override a provider's availability.  'disabled' stops every launch against it; 'available' forces it
    launchable against the evidence and always expires; 'auto' clears the override, resets the failure
    counters and re-derives the state from evidence.  An override lasts --for a duration (90s, 30m, 4h,
    2d) or --until a timestamp, else provider_failover.override.default_ttl_seconds, and never longer
    than override.max_ttl_seconds; --no-expiry is accepted for 'disabled' only.  Operators and
    supervisors only.

    Args:
        body (ProviderSetStateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProviderSetStateResponse | ProviderSetStateResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderSetStateRequest,
) -> Response[ProviderSetStateResponse | ProviderSetStateResponse422]:
    """Override a provider's availability.  'disabled' stops every launch against it; 'available' forces it
    launchable against the evidence and always expires; 'auto' clears the override, resets the failure
    counters and re-derives the state from evidence.  An override lasts --for a duration (90s, 30m, 4h,
    2d) or --until a timestamp, else provider_failover.override.default_ttl_seconds, and never longer
    than override.max_ttl_seconds; --no-expiry is accepted for 'disabled' only.  Operators and
    supervisors only.

     Override a provider's availability.  'disabled' stops every launch against it; 'available' forces it
    launchable against the evidence and always expires; 'auto' clears the override, resets the failure
    counters and re-derives the state from evidence.  An override lasts --for a duration (90s, 30m, 4h,
    2d) or --until a timestamp, else provider_failover.override.default_ttl_seconds, and never longer
    than override.max_ttl_seconds; --no-expiry is accepted for 'disabled' only.  Operators and
    supervisors only.

    Args:
        body (ProviderSetStateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProviderSetStateResponse | ProviderSetStateResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderSetStateRequest,
) -> ProviderSetStateResponse | ProviderSetStateResponse422 | None:
    """Override a provider's availability.  'disabled' stops every launch against it; 'available' forces it
    launchable against the evidence and always expires; 'auto' clears the override, resets the failure
    counters and re-derives the state from evidence.  An override lasts --for a duration (90s, 30m, 4h,
    2d) or --until a timestamp, else provider_failover.override.default_ttl_seconds, and never longer
    than override.max_ttl_seconds; --no-expiry is accepted for 'disabled' only.  Operators and
    supervisors only.

     Override a provider's availability.  'disabled' stops every launch against it; 'available' forces it
    launchable against the evidence and always expires; 'auto' clears the override, resets the failure
    counters and re-derives the state from evidence.  An override lasts --for a duration (90s, 30m, 4h,
    2d) or --until a timestamp, else provider_failover.override.default_ttl_seconds, and never longer
    than override.max_ttl_seconds; --no-expiry is accepted for 'disabled' only.  Operators and
    supervisors only.

    Args:
        body (ProviderSetStateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProviderSetStateResponse | ProviderSetStateResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
