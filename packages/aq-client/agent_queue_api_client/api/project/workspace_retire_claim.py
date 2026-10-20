from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.workspace_retire_claim_request import WorkspaceRetireClaimRequest
from ...models.workspace_retire_claim_response import WorkspaceRetireClaimResponse
from ...models.workspace_retire_claim_response_422 import WorkspaceRetireClaimResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: WorkspaceRetireClaimRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/project/workspace-retire-claim",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> WorkspaceRetireClaimResponse | WorkspaceRetireClaimResponse422 | None:
    if response.status_code == 200:
        response_200 = WorkspaceRetireClaimResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = WorkspaceRetireClaimResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[WorkspaceRetireClaimResponse | WorkspaceRetireClaimResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceRetireClaimRequest,
) -> Response[WorkspaceRetireClaimResponse | WorkspaceRetireClaimResponse422]:
    """Remove a worktree slot's leftover .aq/claim.json when its holder provably let go: the slot is
    unlocked, the holder task is completed, paused, released or superseded, and no live session works
    there. Refuses a claim that may still be live. Use when claim preparation reports 'retains a foreign
    claim'.

     Remove a worktree slot's leftover .aq/claim.json when its holder provably let go: the slot is
    unlocked, the holder task is completed, paused, released or superseded, and no live session works
    there. Refuses a claim that may still be live. Use when claim preparation reports 'retains a foreign
    claim'.

    Args:
        body (WorkspaceRetireClaimRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[WorkspaceRetireClaimResponse | WorkspaceRetireClaimResponse422]
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
    body: WorkspaceRetireClaimRequest,
) -> WorkspaceRetireClaimResponse | WorkspaceRetireClaimResponse422 | None:
    """Remove a worktree slot's leftover .aq/claim.json when its holder provably let go: the slot is
    unlocked, the holder task is completed, paused, released or superseded, and no live session works
    there. Refuses a claim that may still be live. Use when claim preparation reports 'retains a foreign
    claim'.

     Remove a worktree slot's leftover .aq/claim.json when its holder provably let go: the slot is
    unlocked, the holder task is completed, paused, released or superseded, and no live session works
    there. Refuses a claim that may still be live. Use when claim preparation reports 'retains a foreign
    claim'.

    Args:
        body (WorkspaceRetireClaimRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        WorkspaceRetireClaimResponse | WorkspaceRetireClaimResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceRetireClaimRequest,
) -> Response[WorkspaceRetireClaimResponse | WorkspaceRetireClaimResponse422]:
    """Remove a worktree slot's leftover .aq/claim.json when its holder provably let go: the slot is
    unlocked, the holder task is completed, paused, released or superseded, and no live session works
    there. Refuses a claim that may still be live. Use when claim preparation reports 'retains a foreign
    claim'.

     Remove a worktree slot's leftover .aq/claim.json when its holder provably let go: the slot is
    unlocked, the holder task is completed, paused, released or superseded, and no live session works
    there. Refuses a claim that may still be live. Use when claim preparation reports 'retains a foreign
    claim'.

    Args:
        body (WorkspaceRetireClaimRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[WorkspaceRetireClaimResponse | WorkspaceRetireClaimResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: WorkspaceRetireClaimRequest,
) -> WorkspaceRetireClaimResponse | WorkspaceRetireClaimResponse422 | None:
    """Remove a worktree slot's leftover .aq/claim.json when its holder provably let go: the slot is
    unlocked, the holder task is completed, paused, released or superseded, and no live session works
    there. Refuses a claim that may still be live. Use when claim preparation reports 'retains a foreign
    claim'.

     Remove a worktree slot's leftover .aq/claim.json when its holder provably let go: the slot is
    unlocked, the holder task is completed, paused, released or superseded, and no live session works
    there. Refuses a claim that may still be live. Use when claim preparation reports 'retains a foreign
    claim'.

    Args:
        body (WorkspaceRetireClaimRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        WorkspaceRetireClaimResponse | WorkspaceRetireClaimResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
