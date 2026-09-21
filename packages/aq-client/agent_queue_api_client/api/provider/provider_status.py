from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.provider_status_request import ProviderStatusRequest
from ...models.provider_status_response import ProviderStatusResponse
from ...models.provider_status_response_422 import ProviderStatusResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ProviderStatusRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/provider/status",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ProviderStatusResponse | ProviderStatusResponse422 | None:
    if response.status_code == 200:
        response_200 = ProviderStatusResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ProviderStatusResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ProviderStatusResponse | ProviderStatusResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderStatusRequest,
) -> Response[ProviderStatusResponse | ProviderStatusResponse422]:
    """Show each provider's availability: the effective state (available, degraded, exhausted,
    unauthenticated, failing, disabled), its reason, since when, the expected recovery, any operator
    override and its expiry, how many queued tasks it is holding, the last successful launch and the
    newest account-wide usage reading.  A provider is the harness login (claude, codex); a vendor name
    (openai, anthropic) is accepted as an alias.  --verbose adds the evidence ring and the last ten
    transitions.

     Show each provider's availability: the effective state (available, degraded, exhausted,
    unauthenticated, failing, disabled), its reason, since when, the expected recovery, any operator
    override and its expiry, how many queued tasks it is holding, the last successful launch and the
    newest account-wide usage reading.  A provider is the harness login (claude, codex); a vendor name
    (openai, anthropic) is accepted as an alias.  --verbose adds the evidence ring and the last ten
    transitions.

    Args:
        body (ProviderStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProviderStatusResponse | ProviderStatusResponse422]
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
    body: ProviderStatusRequest,
) -> ProviderStatusResponse | ProviderStatusResponse422 | None:
    """Show each provider's availability: the effective state (available, degraded, exhausted,
    unauthenticated, failing, disabled), its reason, since when, the expected recovery, any operator
    override and its expiry, how many queued tasks it is holding, the last successful launch and the
    newest account-wide usage reading.  A provider is the harness login (claude, codex); a vendor name
    (openai, anthropic) is accepted as an alias.  --verbose adds the evidence ring and the last ten
    transitions.

     Show each provider's availability: the effective state (available, degraded, exhausted,
    unauthenticated, failing, disabled), its reason, since when, the expected recovery, any operator
    override and its expiry, how many queued tasks it is holding, the last successful launch and the
    newest account-wide usage reading.  A provider is the harness login (claude, codex); a vendor name
    (openai, anthropic) is accepted as an alias.  --verbose adds the evidence ring and the last ten
    transitions.

    Args:
        body (ProviderStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProviderStatusResponse | ProviderStatusResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderStatusRequest,
) -> Response[ProviderStatusResponse | ProviderStatusResponse422]:
    """Show each provider's availability: the effective state (available, degraded, exhausted,
    unauthenticated, failing, disabled), its reason, since when, the expected recovery, any operator
    override and its expiry, how many queued tasks it is holding, the last successful launch and the
    newest account-wide usage reading.  A provider is the harness login (claude, codex); a vendor name
    (openai, anthropic) is accepted as an alias.  --verbose adds the evidence ring and the last ten
    transitions.

     Show each provider's availability: the effective state (available, degraded, exhausted,
    unauthenticated, failing, disabled), its reason, since when, the expected recovery, any operator
    override and its expiry, how many queued tasks it is holding, the last successful launch and the
    newest account-wide usage reading.  A provider is the harness login (claude, codex); a vendor name
    (openai, anthropic) is accepted as an alias.  --verbose adds the evidence ring and the last ten
    transitions.

    Args:
        body (ProviderStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProviderStatusResponse | ProviderStatusResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderStatusRequest,
) -> ProviderStatusResponse | ProviderStatusResponse422 | None:
    """Show each provider's availability: the effective state (available, degraded, exhausted,
    unauthenticated, failing, disabled), its reason, since when, the expected recovery, any operator
    override and its expiry, how many queued tasks it is holding, the last successful launch and the
    newest account-wide usage reading.  A provider is the harness login (claude, codex); a vendor name
    (openai, anthropic) is accepted as an alias.  --verbose adds the evidence ring and the last ten
    transitions.

     Show each provider's availability: the effective state (available, degraded, exhausted,
    unauthenticated, failing, disabled), its reason, since when, the expected recovery, any operator
    override and its expiry, how many queued tasks it is holding, the last successful launch and the
    newest account-wide usage reading.  A provider is the harness login (claude, codex); a vendor name
    (openai, anthropic) is accepted as an alias.  --verbose adds the evidence ring and the last ten
    transitions.

    Args:
        body (ProviderStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProviderStatusResponse | ProviderStatusResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
