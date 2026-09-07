from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.gate_create_request import GateCreateRequest
from ...models.gate_create_response import GateCreateResponse
from ...models.gate_create_response_422 import GateCreateResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: GateCreateRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/gate-create",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> GateCreateResponse | GateCreateResponse422 | None:
    if response.status_code == 200:
        response_200 = GateCreateResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = GateCreateResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[GateCreateResponse | GateCreateResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GateCreateRequest,
) -> Response[GateCreateResponse | GateCreateResponse422]:
    """Open a gate, and block the tasks waiting on it until it resolves.

     Open a gate, and block the tasks waiting on it until it resolves.

    Args:
        body (GateCreateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GateCreateResponse | GateCreateResponse422]
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
    body: GateCreateRequest,
) -> GateCreateResponse | GateCreateResponse422 | None:
    """Open a gate, and block the tasks waiting on it until it resolves.

     Open a gate, and block the tasks waiting on it until it resolves.

    Args:
        body (GateCreateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GateCreateResponse | GateCreateResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GateCreateRequest,
) -> Response[GateCreateResponse | GateCreateResponse422]:
    """Open a gate, and block the tasks waiting on it until it resolves.

     Open a gate, and block the tasks waiting on it until it resolves.

    Args:
        body (GateCreateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GateCreateResponse | GateCreateResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: GateCreateRequest,
) -> GateCreateResponse | GateCreateResponse422 | None:
    """Open a gate, and block the tasks waiting on it until it resolves.

     Open a gate, and block the tasks waiting on it until it resolves.

    Args:
        body (GateCreateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GateCreateResponse | GateCreateResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
