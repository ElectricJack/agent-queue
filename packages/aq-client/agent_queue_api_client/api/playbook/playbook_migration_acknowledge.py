from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_migration_ack_response import PlaybookMigrationAckResponse
from ...models.playbook_migration_acknowledge_request import PlaybookMigrationAcknowledgeRequest
from ...models.playbook_migration_acknowledge_response_422 import PlaybookMigrationAcknowledgeResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookMigrationAcknowledgeRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/migration-acknowledge",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookMigrationAckResponse | PlaybookMigrationAcknowledgeResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookMigrationAckResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookMigrationAcknowledgeResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookMigrationAckResponse | PlaybookMigrationAcknowledgeResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookMigrationAcknowledgeRequest,
) -> Response[PlaybookMigrationAckResponse | PlaybookMigrationAcknowledgeResponse422]:
    """Record a written operator waiver that one playbook cannot be migrated to V2, so cutover may proceed
    without it. Operator-only. The waiver binds to the source bytes present now, so editing the
    authoring markdown invalidates it.

     Record a written operator waiver that one playbook cannot be migrated to V2, so cutover may proceed
    without it. Operator-only. The waiver binds to the source bytes present now, so editing the
    authoring markdown invalidates it.

    Args:
        body (PlaybookMigrationAcknowledgeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookMigrationAckResponse | PlaybookMigrationAcknowledgeResponse422]
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
    body: PlaybookMigrationAcknowledgeRequest,
) -> PlaybookMigrationAckResponse | PlaybookMigrationAcknowledgeResponse422 | None:
    """Record a written operator waiver that one playbook cannot be migrated to V2, so cutover may proceed
    without it. Operator-only. The waiver binds to the source bytes present now, so editing the
    authoring markdown invalidates it.

     Record a written operator waiver that one playbook cannot be migrated to V2, so cutover may proceed
    without it. Operator-only. The waiver binds to the source bytes present now, so editing the
    authoring markdown invalidates it.

    Args:
        body (PlaybookMigrationAcknowledgeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookMigrationAckResponse | PlaybookMigrationAcknowledgeResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookMigrationAcknowledgeRequest,
) -> Response[PlaybookMigrationAckResponse | PlaybookMigrationAcknowledgeResponse422]:
    """Record a written operator waiver that one playbook cannot be migrated to V2, so cutover may proceed
    without it. Operator-only. The waiver binds to the source bytes present now, so editing the
    authoring markdown invalidates it.

     Record a written operator waiver that one playbook cannot be migrated to V2, so cutover may proceed
    without it. Operator-only. The waiver binds to the source bytes present now, so editing the
    authoring markdown invalidates it.

    Args:
        body (PlaybookMigrationAcknowledgeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookMigrationAckResponse | PlaybookMigrationAcknowledgeResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookMigrationAcknowledgeRequest,
) -> PlaybookMigrationAckResponse | PlaybookMigrationAcknowledgeResponse422 | None:
    """Record a written operator waiver that one playbook cannot be migrated to V2, so cutover may proceed
    without it. Operator-only. The waiver binds to the source bytes present now, so editing the
    authoring markdown invalidates it.

     Record a written operator waiver that one playbook cannot be migrated to V2, so cutover may proceed
    without it. Operator-only. The waiver binds to the source bytes present now, so editing the
    authoring markdown invalidates it.

    Args:
        body (PlaybookMigrationAcknowledgeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookMigrationAckResponse | PlaybookMigrationAcknowledgeResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
