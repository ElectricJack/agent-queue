from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.pool_scale_request import PoolScaleRequest
from ...models.pool_scale_response import PoolScaleResponse
from ...models.pool_scale_response_422 import PoolScaleResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PoolScaleRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/pool/scale",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PoolScaleResponse | PoolScaleResponse422 | None:
    if response.status_code == 200:
        response_200 = PoolScaleResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PoolScaleResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PoolScaleResponse | PoolScaleResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PoolScaleRequest,
) -> Response[PoolScaleResponse | PoolScaleResponse422]:
    """Set a pool profile's min/max active-session bounds on the system profile (they apply to every
    project; each project's max_concurrent_agents still caps its own pool). Validates min >= 0, max >=
    1, and max >= min; max may be null for no profile limit. With `now: true`, also terminates idle
    sessions above the effective max, oldest first. Backs `aq pool scale`.

     Set a pool profile's min/max active-session bounds on the system profile (they apply to every
    project; each project's max_concurrent_agents still caps its own pool). Validates min >= 0, max >=
    1, and max >= min; max may be null for no profile limit. With `now: true`, also terminates idle
    sessions above the effective max, oldest first. Backs `aq pool scale`.

    Args:
        body (PoolScaleRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PoolScaleResponse | PoolScaleResponse422]
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
    body: PoolScaleRequest,
) -> PoolScaleResponse | PoolScaleResponse422 | None:
    """Set a pool profile's min/max active-session bounds on the system profile (they apply to every
    project; each project's max_concurrent_agents still caps its own pool). Validates min >= 0, max >=
    1, and max >= min; max may be null for no profile limit. With `now: true`, also terminates idle
    sessions above the effective max, oldest first. Backs `aq pool scale`.

     Set a pool profile's min/max active-session bounds on the system profile (they apply to every
    project; each project's max_concurrent_agents still caps its own pool). Validates min >= 0, max >=
    1, and max >= min; max may be null for no profile limit. With `now: true`, also terminates idle
    sessions above the effective max, oldest first. Backs `aq pool scale`.

    Args:
        body (PoolScaleRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PoolScaleResponse | PoolScaleResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PoolScaleRequest,
) -> Response[PoolScaleResponse | PoolScaleResponse422]:
    """Set a pool profile's min/max active-session bounds on the system profile (they apply to every
    project; each project's max_concurrent_agents still caps its own pool). Validates min >= 0, max >=
    1, and max >= min; max may be null for no profile limit. With `now: true`, also terminates idle
    sessions above the effective max, oldest first. Backs `aq pool scale`.

     Set a pool profile's min/max active-session bounds on the system profile (they apply to every
    project; each project's max_concurrent_agents still caps its own pool). Validates min >= 0, max >=
    1, and max >= min; max may be null for no profile limit. With `now: true`, also terminates idle
    sessions above the effective max, oldest first. Backs `aq pool scale`.

    Args:
        body (PoolScaleRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PoolScaleResponse | PoolScaleResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PoolScaleRequest,
) -> PoolScaleResponse | PoolScaleResponse422 | None:
    """Set a pool profile's min/max active-session bounds on the system profile (they apply to every
    project; each project's max_concurrent_agents still caps its own pool). Validates min >= 0, max >=
    1, and max >= min; max may be null for no profile limit. With `now: true`, also terminates idle
    sessions above the effective max, oldest first. Backs `aq pool scale`.

     Set a pool profile's min/max active-session bounds on the system profile (they apply to every
    project; each project's max_concurrent_agents still caps its own pool). Validates min >= 0, max >=
    1, and max >= min; max may be null for no profile limit. With `now: true`, also terminates idle
    sessions above the effective max, oldest first. Backs `aq pool scale`.

    Args:
        body (PoolScaleRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PoolScaleResponse | PoolScaleResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
