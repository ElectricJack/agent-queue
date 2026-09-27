from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.provider_allocation_apply_request import ProviderAllocationApplyRequest
from ...models.provider_allocation_apply_response import ProviderAllocationApplyResponse
from ...models.provider_allocation_apply_response_422 import ProviderAllocationApplyResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ProviderAllocationApplyRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/provider/allocation-apply",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ProviderAllocationApplyResponse | ProviderAllocationApplyResponse422 | None:
    if response.status_code == 200:
        response_200 = ProviderAllocationApplyResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ProviderAllocationApplyResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ProviderAllocationApplyResponse | ProviderAllocationApplyResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderAllocationApplyRequest,
) -> Response[ProviderAllocationApplyResponse | ProviderAllocationApplyResponse422]:
    """Apply a reviewed provider allocation preview by its preview_token (no other selector: the request is
    the one the token was issued for).  Refused with error_code preview_stale and a fresh preview when
    anything the preview observed has changed, so the applied set is the previewed set.  Profile
    lifecycle and bounds edits run in profile-id order through the same code as pool_set_lifecycle and
    pool_scale; a failure compensates every earlier edit and is reported (status rolled_back or
    partial), never as success.  A prefer preference re-places the project's queued class_only READY
    tasks onto the provider.  Drains: graceful lets busy work finish, idle-now also stops idle workers
    now, interrupt-busy (operator only) also interrupts exactly the authorized busy set.  Records one
    provider.allocation_changed event.

     Apply a reviewed provider allocation preview by its preview_token (no other selector: the request is
    the one the token was issued for).  Refused with error_code preview_stale and a fresh preview when
    anything the preview observed has changed, so the applied set is the previewed set.  Profile
    lifecycle and bounds edits run in profile-id order through the same code as pool_set_lifecycle and
    pool_scale; a failure compensates every earlier edit and is reported (status rolled_back or
    partial), never as success.  A prefer preference re-places the project's queued class_only READY
    tasks onto the provider.  Drains: graceful lets busy work finish, idle-now also stops idle workers
    now, interrupt-busy (operator only) also interrupts exactly the authorized busy set.  Records one
    provider.allocation_changed event.

    Args:
        body (ProviderAllocationApplyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProviderAllocationApplyResponse | ProviderAllocationApplyResponse422]
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
    body: ProviderAllocationApplyRequest,
) -> ProviderAllocationApplyResponse | ProviderAllocationApplyResponse422 | None:
    """Apply a reviewed provider allocation preview by its preview_token (no other selector: the request is
    the one the token was issued for).  Refused with error_code preview_stale and a fresh preview when
    anything the preview observed has changed, so the applied set is the previewed set.  Profile
    lifecycle and bounds edits run in profile-id order through the same code as pool_set_lifecycle and
    pool_scale; a failure compensates every earlier edit and is reported (status rolled_back or
    partial), never as success.  A prefer preference re-places the project's queued class_only READY
    tasks onto the provider.  Drains: graceful lets busy work finish, idle-now also stops idle workers
    now, interrupt-busy (operator only) also interrupts exactly the authorized busy set.  Records one
    provider.allocation_changed event.

     Apply a reviewed provider allocation preview by its preview_token (no other selector: the request is
    the one the token was issued for).  Refused with error_code preview_stale and a fresh preview when
    anything the preview observed has changed, so the applied set is the previewed set.  Profile
    lifecycle and bounds edits run in profile-id order through the same code as pool_set_lifecycle and
    pool_scale; a failure compensates every earlier edit and is reported (status rolled_back or
    partial), never as success.  A prefer preference re-places the project's queued class_only READY
    tasks onto the provider.  Drains: graceful lets busy work finish, idle-now also stops idle workers
    now, interrupt-busy (operator only) also interrupts exactly the authorized busy set.  Records one
    provider.allocation_changed event.

    Args:
        body (ProviderAllocationApplyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProviderAllocationApplyResponse | ProviderAllocationApplyResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderAllocationApplyRequest,
) -> Response[ProviderAllocationApplyResponse | ProviderAllocationApplyResponse422]:
    """Apply a reviewed provider allocation preview by its preview_token (no other selector: the request is
    the one the token was issued for).  Refused with error_code preview_stale and a fresh preview when
    anything the preview observed has changed, so the applied set is the previewed set.  Profile
    lifecycle and bounds edits run in profile-id order through the same code as pool_set_lifecycle and
    pool_scale; a failure compensates every earlier edit and is reported (status rolled_back or
    partial), never as success.  A prefer preference re-places the project's queued class_only READY
    tasks onto the provider.  Drains: graceful lets busy work finish, idle-now also stops idle workers
    now, interrupt-busy (operator only) also interrupts exactly the authorized busy set.  Records one
    provider.allocation_changed event.

     Apply a reviewed provider allocation preview by its preview_token (no other selector: the request is
    the one the token was issued for).  Refused with error_code preview_stale and a fresh preview when
    anything the preview observed has changed, so the applied set is the previewed set.  Profile
    lifecycle and bounds edits run in profile-id order through the same code as pool_set_lifecycle and
    pool_scale; a failure compensates every earlier edit and is reported (status rolled_back or
    partial), never as success.  A prefer preference re-places the project's queued class_only READY
    tasks onto the provider.  Drains: graceful lets busy work finish, idle-now also stops idle workers
    now, interrupt-busy (operator only) also interrupts exactly the authorized busy set.  Records one
    provider.allocation_changed event.

    Args:
        body (ProviderAllocationApplyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProviderAllocationApplyResponse | ProviderAllocationApplyResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderAllocationApplyRequest,
) -> ProviderAllocationApplyResponse | ProviderAllocationApplyResponse422 | None:
    """Apply a reviewed provider allocation preview by its preview_token (no other selector: the request is
    the one the token was issued for).  Refused with error_code preview_stale and a fresh preview when
    anything the preview observed has changed, so the applied set is the previewed set.  Profile
    lifecycle and bounds edits run in profile-id order through the same code as pool_set_lifecycle and
    pool_scale; a failure compensates every earlier edit and is reported (status rolled_back or
    partial), never as success.  A prefer preference re-places the project's queued class_only READY
    tasks onto the provider.  Drains: graceful lets busy work finish, idle-now also stops idle workers
    now, interrupt-busy (operator only) also interrupts exactly the authorized busy set.  Records one
    provider.allocation_changed event.

     Apply a reviewed provider allocation preview by its preview_token (no other selector: the request is
    the one the token was issued for).  Refused with error_code preview_stale and a fresh preview when
    anything the preview observed has changed, so the applied set is the previewed set.  Profile
    lifecycle and bounds edits run in profile-id order through the same code as pool_set_lifecycle and
    pool_scale; a failure compensates every earlier edit and is reported (status rolled_back or
    partial), never as success.  A prefer preference re-places the project's queued class_only READY
    tasks onto the provider.  Drains: graceful lets busy work finish, idle-now also stops idle workers
    now, interrupt-busy (operator only) also interrupts exactly the authorized busy set.  Records one
    provider.allocation_changed event.

    Args:
        body (ProviderAllocationApplyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProviderAllocationApplyResponse | ProviderAllocationApplyResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
