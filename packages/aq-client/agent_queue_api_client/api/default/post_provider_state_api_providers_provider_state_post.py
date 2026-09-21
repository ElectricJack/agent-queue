from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.http_validation_error import HTTPValidationError
from ...models.provider_set_state_response import ProviderSetStateResponse
from ...models.provider_state_request import ProviderStateRequest
from ...types import Response


def _get_kwargs(
    provider: str,
    *,
    body: ProviderStateRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/providers/{provider}/state".format(
            provider=quote(str(provider), safe=""),
        ),
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | HTTPValidationError | ProviderSetStateResponse | None:
    if response.status_code == 200:
        response_200 = ProviderSetStateResponse.from_dict(response.json())

        return response_200

    if response.status_code == 400:
        response_400 = cast(Any, None)
        return response_400

    if response.status_code == 403:
        response_403 = cast(Any, None)
        return response_403

    if response.status_code == 404:
        response_404 = cast(Any, None)
        return response_404

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Any | HTTPValidationError | ProviderSetStateResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    provider: str,
    *,
    client: AuthenticatedClient | Client,
    body: ProviderStateRequest,
) -> Response[Any | HTTPValidationError | ProviderSetStateResponse]:
    """Post Provider State

    Args:
        provider (str):
        body (ProviderStateRequest): ``POST /api/providers/{provider}/state`` (D6).

            ``for`` is a duration (``90s``, ``30m``, ``4h``, ``2d``, or seconds);
            ``until`` an epoch or ISO-8601 time.  Give at most one of ``for``,
            ``until`` and ``no_expiry``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | HTTPValidationError | ProviderSetStateResponse]
    """

    kwargs = _get_kwargs(
        provider=provider,
        body=body,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    provider: str,
    *,
    client: AuthenticatedClient | Client,
    body: ProviderStateRequest,
) -> Any | HTTPValidationError | ProviderSetStateResponse | None:
    """Post Provider State

    Args:
        provider (str):
        body (ProviderStateRequest): ``POST /api/providers/{provider}/state`` (D6).

            ``for`` is a duration (``90s``, ``30m``, ``4h``, ``2d``, or seconds);
            ``until`` an epoch or ISO-8601 time.  Give at most one of ``for``,
            ``until`` and ``no_expiry``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | HTTPValidationError | ProviderSetStateResponse
    """

    return sync_detailed(
        provider=provider,
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    provider: str,
    *,
    client: AuthenticatedClient | Client,
    body: ProviderStateRequest,
) -> Response[Any | HTTPValidationError | ProviderSetStateResponse]:
    """Post Provider State

    Args:
        provider (str):
        body (ProviderStateRequest): ``POST /api/providers/{provider}/state`` (D6).

            ``for`` is a duration (``90s``, ``30m``, ``4h``, ``2d``, or seconds);
            ``until`` an epoch or ISO-8601 time.  Give at most one of ``for``,
            ``until`` and ``no_expiry``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | HTTPValidationError | ProviderSetStateResponse]
    """

    kwargs = _get_kwargs(
        provider=provider,
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    provider: str,
    *,
    client: AuthenticatedClient | Client,
    body: ProviderStateRequest,
) -> Any | HTTPValidationError | ProviderSetStateResponse | None:
    """Post Provider State

    Args:
        provider (str):
        body (ProviderStateRequest): ``POST /api/providers/{provider}/state`` (D6).

            ``for`` is a duration (``90s``, ``30m``, ``4h``, ``2d``, or seconds);
            ``until`` an epoch or ISO-8601 time.  Give at most one of ``for``,
            ``until`` and ``no_expiry``.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | HTTPValidationError | ProviderSetStateResponse
    """

    return (
        await asyncio_detailed(
            provider=provider,
            client=client,
            body=body,
        )
    ).parsed
