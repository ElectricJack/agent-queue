from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.provider_recheck_request import ProviderRecheckRequest
from ...models.provider_recheck_response import ProviderRecheckResponse
from ...models.provider_recheck_response_422 import ProviderRecheckResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ProviderRecheckRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/provider/recheck",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ProviderRecheckResponse | ProviderRecheckResponse422 | None:
    if response.status_code == 200:
        response_200 = ProviderRecheckResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ProviderRecheckResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ProviderRecheckResponse | ProviderRecheckResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderRecheckRequest,
) -> Response[ProviderRecheckResponse | ProviderRecheckResponse422]:
    """Run the provider's login probe now (e.g. `codex login status`) and fold the answer into its state.
    An authenticated answer moves an unauthenticated provider to probation; the next successful launch
    completes recovery.  Operators and supervisors only.

     Run the provider's login probe now (e.g. `codex login status`) and fold the answer into its state.
    An authenticated answer moves an unauthenticated provider to probation; the next successful launch
    completes recovery.  Operators and supervisors only.

    Args:
        body (ProviderRecheckRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProviderRecheckResponse | ProviderRecheckResponse422]
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
    body: ProviderRecheckRequest,
) -> ProviderRecheckResponse | ProviderRecheckResponse422 | None:
    """Run the provider's login probe now (e.g. `codex login status`) and fold the answer into its state.
    An authenticated answer moves an unauthenticated provider to probation; the next successful launch
    completes recovery.  Operators and supervisors only.

     Run the provider's login probe now (e.g. `codex login status`) and fold the answer into its state.
    An authenticated answer moves an unauthenticated provider to probation; the next successful launch
    completes recovery.  Operators and supervisors only.

    Args:
        body (ProviderRecheckRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProviderRecheckResponse | ProviderRecheckResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderRecheckRequest,
) -> Response[ProviderRecheckResponse | ProviderRecheckResponse422]:
    """Run the provider's login probe now (e.g. `codex login status`) and fold the answer into its state.
    An authenticated answer moves an unauthenticated provider to probation; the next successful launch
    completes recovery.  Operators and supervisors only.

     Run the provider's login probe now (e.g. `codex login status`) and fold the answer into its state.
    An authenticated answer moves an unauthenticated provider to probation; the next successful launch
    completes recovery.  Operators and supervisors only.

    Args:
        body (ProviderRecheckRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProviderRecheckResponse | ProviderRecheckResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderRecheckRequest,
) -> ProviderRecheckResponse | ProviderRecheckResponse422 | None:
    """Run the provider's login probe now (e.g. `codex login status`) and fold the answer into its state.
    An authenticated answer moves an unauthenticated provider to probation; the next successful launch
    completes recovery.  Operators and supervisors only.

     Run the provider's login probe now (e.g. `codex login status`) and fold the answer into its state.
    An authenticated answer moves an unauthenticated provider to probation; the next successful launch
    completes recovery.  Operators and supervisors only.

    Args:
        body (ProviderRecheckRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProviderRecheckResponse | ProviderRecheckResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
