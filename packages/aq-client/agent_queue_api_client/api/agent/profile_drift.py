from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.profile_drift_request import ProfileDriftRequest
from ...models.profile_drift_response import ProfileDriftResponse
from ...models.profile_drift_response_422 import ProfileDriftResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ProfileDriftRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/agent/profile-drift",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ProfileDriftResponse | ProfileDriftResponse422 | None:
    if response.status_code == 200:
        response_200 = ProfileDriftResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ProfileDriftResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ProfileDriftResponse | ProfileDriftResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProfileDriftRequest,
) -> Response[ProfileDriftResponse | ProfileDriftResponse422]:
    """Report which vault system profiles have drifted from the defaults shipped in src/profiles/defaults/.
    Startup seeding never overwrites an existing vault profile.md, so an old copy keeps old semantics: a
    stale read_only re-arms the require-a-PR close gate. Reports divergence on the semantic Config
    fields (read_only, harness, lifecycle, needs_workspace) and missing/renamed sections. Read-only —
    repair with profile_reseed.

     Report which vault system profiles have drifted from the defaults shipped in src/profiles/defaults/.
    Startup seeding never overwrites an existing vault profile.md, so an old copy keeps old semantics: a
    stale read_only re-arms the require-a-PR close gate. Reports divergence on the semantic Config
    fields (read_only, harness, lifecycle, needs_workspace) and missing/renamed sections. Read-only —
    repair with profile_reseed.

    Args:
        body (ProfileDriftRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProfileDriftResponse | ProfileDriftResponse422]
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
    body: ProfileDriftRequest,
) -> ProfileDriftResponse | ProfileDriftResponse422 | None:
    """Report which vault system profiles have drifted from the defaults shipped in src/profiles/defaults/.
    Startup seeding never overwrites an existing vault profile.md, so an old copy keeps old semantics: a
    stale read_only re-arms the require-a-PR close gate. Reports divergence on the semantic Config
    fields (read_only, harness, lifecycle, needs_workspace) and missing/renamed sections. Read-only —
    repair with profile_reseed.

     Report which vault system profiles have drifted from the defaults shipped in src/profiles/defaults/.
    Startup seeding never overwrites an existing vault profile.md, so an old copy keeps old semantics: a
    stale read_only re-arms the require-a-PR close gate. Reports divergence on the semantic Config
    fields (read_only, harness, lifecycle, needs_workspace) and missing/renamed sections. Read-only —
    repair with profile_reseed.

    Args:
        body (ProfileDriftRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProfileDriftResponse | ProfileDriftResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProfileDriftRequest,
) -> Response[ProfileDriftResponse | ProfileDriftResponse422]:
    """Report which vault system profiles have drifted from the defaults shipped in src/profiles/defaults/.
    Startup seeding never overwrites an existing vault profile.md, so an old copy keeps old semantics: a
    stale read_only re-arms the require-a-PR close gate. Reports divergence on the semantic Config
    fields (read_only, harness, lifecycle, needs_workspace) and missing/renamed sections. Read-only —
    repair with profile_reseed.

     Report which vault system profiles have drifted from the defaults shipped in src/profiles/defaults/.
    Startup seeding never overwrites an existing vault profile.md, so an old copy keeps old semantics: a
    stale read_only re-arms the require-a-PR close gate. Reports divergence on the semantic Config
    fields (read_only, harness, lifecycle, needs_workspace) and missing/renamed sections. Read-only —
    repair with profile_reseed.

    Args:
        body (ProfileDriftRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProfileDriftResponse | ProfileDriftResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ProfileDriftRequest,
) -> ProfileDriftResponse | ProfileDriftResponse422 | None:
    """Report which vault system profiles have drifted from the defaults shipped in src/profiles/defaults/.
    Startup seeding never overwrites an existing vault profile.md, so an old copy keeps old semantics: a
    stale read_only re-arms the require-a-PR close gate. Reports divergence on the semantic Config
    fields (read_only, harness, lifecycle, needs_workspace) and missing/renamed sections. Read-only —
    repair with profile_reseed.

     Report which vault system profiles have drifted from the defaults shipped in src/profiles/defaults/.
    Startup seeding never overwrites an existing vault profile.md, so an old copy keeps old semantics: a
    stale read_only re-arms the require-a-PR close gate. Reports divergence on the semantic Config
    fields (read_only, harness, lifecycle, needs_workspace) and missing/renamed sections. Read-only —
    repair with profile_reseed.

    Args:
        body (ProfileDriftRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProfileDriftResponse | ProfileDriftResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
