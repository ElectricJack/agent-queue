from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.provider_allocation_status_request import ProviderAllocationStatusRequest
from ...models.provider_allocation_status_response import ProviderAllocationStatusResponse
from ...models.provider_allocation_status_response_422 import ProviderAllocationStatusResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ProviderAllocationStatusRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/provider/allocation-status",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ProviderAllocationStatusResponse | ProviderAllocationStatusResponse422 | None:
    if response.status_code == 200:
        response_200 = ProviderAllocationStatusResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ProviderAllocationStatusResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ProviderAllocationStatusResponse | ProviderAllocationStatusResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderAllocationStatusRequest,
) -> Response[ProviderAllocationStatusResponse | ProviderAllocationStatusResponse422]:
    """Show every ordinary worker profile grouped by provider (the harness login: claude, codex):
    lifecycle, per-profile bounds, class and enabled flag; fleet and per-project pool supply (ready,
    idle, busy, starting, draining, unresponsive); live sessions with their task and idle age;
    READY/ASSIGNED/IN_PROGRESS tasks pinned or preferred to each profile; manual agent definitions and
    their overrides; each project's preferred provider; and the provider-wide configured pool ceiling.
    Control, named, template, malformed and unknown-provider profiles are listed as diagnostics.  Read-
    only; a project-scoped caller sees other projects' sessions and tasks redacted.

     Show every ordinary worker profile grouped by provider (the harness login: claude, codex):
    lifecycle, per-profile bounds, class and enabled flag; fleet and per-project pool supply (ready,
    idle, busy, starting, draining, unresponsive); live sessions with their task and idle age;
    READY/ASSIGNED/IN_PROGRESS tasks pinned or preferred to each profile; manual agent definitions and
    their overrides; each project's preferred provider; and the provider-wide configured pool ceiling.
    Control, named, template, malformed and unknown-provider profiles are listed as diagnostics.  Read-
    only; a project-scoped caller sees other projects' sessions and tasks redacted.

    Args:
        body (ProviderAllocationStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProviderAllocationStatusResponse | ProviderAllocationStatusResponse422]
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
    body: ProviderAllocationStatusRequest,
) -> ProviderAllocationStatusResponse | ProviderAllocationStatusResponse422 | None:
    """Show every ordinary worker profile grouped by provider (the harness login: claude, codex):
    lifecycle, per-profile bounds, class and enabled flag; fleet and per-project pool supply (ready,
    idle, busy, starting, draining, unresponsive); live sessions with their task and idle age;
    READY/ASSIGNED/IN_PROGRESS tasks pinned or preferred to each profile; manual agent definitions and
    their overrides; each project's preferred provider; and the provider-wide configured pool ceiling.
    Control, named, template, malformed and unknown-provider profiles are listed as diagnostics.  Read-
    only; a project-scoped caller sees other projects' sessions and tasks redacted.

     Show every ordinary worker profile grouped by provider (the harness login: claude, codex):
    lifecycle, per-profile bounds, class and enabled flag; fleet and per-project pool supply (ready,
    idle, busy, starting, draining, unresponsive); live sessions with their task and idle age;
    READY/ASSIGNED/IN_PROGRESS tasks pinned or preferred to each profile; manual agent definitions and
    their overrides; each project's preferred provider; and the provider-wide configured pool ceiling.
    Control, named, template, malformed and unknown-provider profiles are listed as diagnostics.  Read-
    only; a project-scoped caller sees other projects' sessions and tasks redacted.

    Args:
        body (ProviderAllocationStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProviderAllocationStatusResponse | ProviderAllocationStatusResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderAllocationStatusRequest,
) -> Response[ProviderAllocationStatusResponse | ProviderAllocationStatusResponse422]:
    """Show every ordinary worker profile grouped by provider (the harness login: claude, codex):
    lifecycle, per-profile bounds, class and enabled flag; fleet and per-project pool supply (ready,
    idle, busy, starting, draining, unresponsive); live sessions with their task and idle age;
    READY/ASSIGNED/IN_PROGRESS tasks pinned or preferred to each profile; manual agent definitions and
    their overrides; each project's preferred provider; and the provider-wide configured pool ceiling.
    Control, named, template, malformed and unknown-provider profiles are listed as diagnostics.  Read-
    only; a project-scoped caller sees other projects' sessions and tasks redacted.

     Show every ordinary worker profile grouped by provider (the harness login: claude, codex):
    lifecycle, per-profile bounds, class and enabled flag; fleet and per-project pool supply (ready,
    idle, busy, starting, draining, unresponsive); live sessions with their task and idle age;
    READY/ASSIGNED/IN_PROGRESS tasks pinned or preferred to each profile; manual agent definitions and
    their overrides; each project's preferred provider; and the provider-wide configured pool ceiling.
    Control, named, template, malformed and unknown-provider profiles are listed as diagnostics.  Read-
    only; a project-scoped caller sees other projects' sessions and tasks redacted.

    Args:
        body (ProviderAllocationStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProviderAllocationStatusResponse | ProviderAllocationStatusResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderAllocationStatusRequest,
) -> ProviderAllocationStatusResponse | ProviderAllocationStatusResponse422 | None:
    """Show every ordinary worker profile grouped by provider (the harness login: claude, codex):
    lifecycle, per-profile bounds, class and enabled flag; fleet and per-project pool supply (ready,
    idle, busy, starting, draining, unresponsive); live sessions with their task and idle age;
    READY/ASSIGNED/IN_PROGRESS tasks pinned or preferred to each profile; manual agent definitions and
    their overrides; each project's preferred provider; and the provider-wide configured pool ceiling.
    Control, named, template, malformed and unknown-provider profiles are listed as diagnostics.  Read-
    only; a project-scoped caller sees other projects' sessions and tasks redacted.

     Show every ordinary worker profile grouped by provider (the harness login: claude, codex):
    lifecycle, per-profile bounds, class and enabled flag; fleet and per-project pool supply (ready,
    idle, busy, starting, draining, unresponsive); live sessions with their task and idle age;
    READY/ASSIGNED/IN_PROGRESS tasks pinned or preferred to each profile; manual agent definitions and
    their overrides; each project's preferred provider; and the provider-wide configured pool ceiling.
    Control, named, template, malformed and unknown-provider profiles are listed as diagnostics.  Read-
    only; a project-scoped caller sees other projects' sessions and tasks redacted.

    Args:
        body (ProviderAllocationStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProviderAllocationStatusResponse | ProviderAllocationStatusResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
