from http import HTTPStatus
from typing import Any, cast

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.http_validation_error import HTTPValidationError
from ...models.provider_status_response import ProviderStatusResponse
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    provider: None | str | Unset = UNSET,
    verbose: bool | Unset = False,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_provider: None | str | Unset
    if isinstance(provider, Unset):
        json_provider = UNSET
    else:
        json_provider = provider
    params["provider"] = json_provider

    params["verbose"] = verbose

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/providers/availability",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | HTTPValidationError | ProviderStatusResponse | None:
    if response.status_code == 200:
        response_200 = ProviderStatusResponse.from_dict(response.json())

        return response_200

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
) -> Response[Any | HTTPValidationError | ProviderStatusResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    provider: None | str | Unset = UNSET,
    verbose: bool | Unset = False,
) -> Response[Any | HTTPValidationError | ProviderStatusResponse]:
    """Get Provider Availability

    Args:
        provider (None | str | Unset):
        verbose (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | HTTPValidationError | ProviderStatusResponse]
    """

    kwargs = _get_kwargs(
        provider=provider,
        verbose=verbose,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    provider: None | str | Unset = UNSET,
    verbose: bool | Unset = False,
) -> Any | HTTPValidationError | ProviderStatusResponse | None:
    """Get Provider Availability

    Args:
        provider (None | str | Unset):
        verbose (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | HTTPValidationError | ProviderStatusResponse
    """

    return sync_detailed(
        client=client,
        provider=provider,
        verbose=verbose,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    provider: None | str | Unset = UNSET,
    verbose: bool | Unset = False,
) -> Response[Any | HTTPValidationError | ProviderStatusResponse]:
    """Get Provider Availability

    Args:
        provider (None | str | Unset):
        verbose (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | HTTPValidationError | ProviderStatusResponse]
    """

    kwargs = _get_kwargs(
        provider=provider,
        verbose=verbose,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    provider: None | str | Unset = UNSET,
    verbose: bool | Unset = False,
) -> Any | HTTPValidationError | ProviderStatusResponse | None:
    """Get Provider Availability

    Args:
        provider (None | str | Unset):
        verbose (bool | Unset):  Default: False.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | HTTPValidationError | ProviderStatusResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            provider=provider,
            verbose=verbose,
        )
    ).parsed
