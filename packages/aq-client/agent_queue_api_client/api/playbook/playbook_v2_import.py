from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_v2_import_request import PlaybookV2ImportRequest
from ...models.playbook_v2_import_response import PlaybookV2ImportResponse
from ...models.playbook_v2_import_response_422 import PlaybookV2ImportResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookV2ImportRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/v2-import",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookV2ImportResponse | PlaybookV2ImportResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookV2ImportResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookV2ImportResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookV2ImportResponse | PlaybookV2ImportResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookV2ImportRequest,
) -> Response[PlaybookV2ImportResponse | PlaybookV2ImportResponse422]:
    """Import one approved Playbook V2 review bundle from inside the vault. Validates the exact canonical
    artifact and source bytes against the recorded review metadata and the daemon's live command,
    profile, and event registries, then stores the content-addressed artifact and its database reference
    atomically. Operator-only. Never activates the artifact; use playbook_activate separately with the
    returned full hash.

     Import one approved Playbook V2 review bundle from inside the vault. Validates the exact canonical
    artifact and source bytes against the recorded review metadata and the daemon's live command,
    profile, and event registries, then stores the content-addressed artifact and its database reference
    atomically. Operator-only. Never activates the artifact; use playbook_activate separately with the
    returned full hash.

    Args:
        body (PlaybookV2ImportRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookV2ImportResponse | PlaybookV2ImportResponse422]
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
    body: PlaybookV2ImportRequest,
) -> PlaybookV2ImportResponse | PlaybookV2ImportResponse422 | None:
    """Import one approved Playbook V2 review bundle from inside the vault. Validates the exact canonical
    artifact and source bytes against the recorded review metadata and the daemon's live command,
    profile, and event registries, then stores the content-addressed artifact and its database reference
    atomically. Operator-only. Never activates the artifact; use playbook_activate separately with the
    returned full hash.

     Import one approved Playbook V2 review bundle from inside the vault. Validates the exact canonical
    artifact and source bytes against the recorded review metadata and the daemon's live command,
    profile, and event registries, then stores the content-addressed artifact and its database reference
    atomically. Operator-only. Never activates the artifact; use playbook_activate separately with the
    returned full hash.

    Args:
        body (PlaybookV2ImportRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookV2ImportResponse | PlaybookV2ImportResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookV2ImportRequest,
) -> Response[PlaybookV2ImportResponse | PlaybookV2ImportResponse422]:
    """Import one approved Playbook V2 review bundle from inside the vault. Validates the exact canonical
    artifact and source bytes against the recorded review metadata and the daemon's live command,
    profile, and event registries, then stores the content-addressed artifact and its database reference
    atomically. Operator-only. Never activates the artifact; use playbook_activate separately with the
    returned full hash.

     Import one approved Playbook V2 review bundle from inside the vault. Validates the exact canonical
    artifact and source bytes against the recorded review metadata and the daemon's live command,
    profile, and event registries, then stores the content-addressed artifact and its database reference
    atomically. Operator-only. Never activates the artifact; use playbook_activate separately with the
    returned full hash.

    Args:
        body (PlaybookV2ImportRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookV2ImportResponse | PlaybookV2ImportResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookV2ImportRequest,
) -> PlaybookV2ImportResponse | PlaybookV2ImportResponse422 | None:
    """Import one approved Playbook V2 review bundle from inside the vault. Validates the exact canonical
    artifact and source bytes against the recorded review metadata and the daemon's live command,
    profile, and event registries, then stores the content-addressed artifact and its database reference
    atomically. Operator-only. Never activates the artifact; use playbook_activate separately with the
    returned full hash.

     Import one approved Playbook V2 review bundle from inside the vault. Validates the exact canonical
    artifact and source bytes against the recorded review metadata and the daemon's live command,
    profile, and event registries, then stores the content-addressed artifact and its database reference
    atomically. Operator-only. Never activates the artifact; use playbook_activate separately with the
    returned full hash.

    Args:
        body (PlaybookV2ImportRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookV2ImportResponse | PlaybookV2ImportResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
