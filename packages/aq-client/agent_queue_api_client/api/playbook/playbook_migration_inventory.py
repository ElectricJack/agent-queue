from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_migration_inventory_request import PlaybookMigrationInventoryRequest
from ...models.playbook_migration_inventory_response import PlaybookMigrationInventoryResponse
from ...models.playbook_migration_inventory_response_422 import PlaybookMigrationInventoryResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookMigrationInventoryRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/migration-inventory",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookMigrationInventoryResponse | PlaybookMigrationInventoryResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookMigrationInventoryResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookMigrationInventoryResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookMigrationInventoryResponse | PlaybookMigrationInventoryResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookMigrationInventoryRequest,
) -> Response[PlaybookMigrationInventoryResponse | PlaybookMigrationInventoryResponse422]:
    """Report every installed playbook's V1->V2 migration readiness: its disposition (ready,
    question_required, invalid, disabled), the operator-facing reasons behind it, the active V2 artifact
    and its activation health. Read-only: it never compiles, activates or writes anything.

     Report every installed playbook's V1->V2 migration readiness: its disposition (ready,
    question_required, invalid, disabled), the operator-facing reasons behind it, the active V2 artifact
    and its activation health. Read-only: it never compiles, activates or writes anything.

    Args:
        body (PlaybookMigrationInventoryRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookMigrationInventoryResponse | PlaybookMigrationInventoryResponse422]
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
    body: PlaybookMigrationInventoryRequest,
) -> PlaybookMigrationInventoryResponse | PlaybookMigrationInventoryResponse422 | None:
    """Report every installed playbook's V1->V2 migration readiness: its disposition (ready,
    question_required, invalid, disabled), the operator-facing reasons behind it, the active V2 artifact
    and its activation health. Read-only: it never compiles, activates or writes anything.

     Report every installed playbook's V1->V2 migration readiness: its disposition (ready,
    question_required, invalid, disabled), the operator-facing reasons behind it, the active V2 artifact
    and its activation health. Read-only: it never compiles, activates or writes anything.

    Args:
        body (PlaybookMigrationInventoryRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookMigrationInventoryResponse | PlaybookMigrationInventoryResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookMigrationInventoryRequest,
) -> Response[PlaybookMigrationInventoryResponse | PlaybookMigrationInventoryResponse422]:
    """Report every installed playbook's V1->V2 migration readiness: its disposition (ready,
    question_required, invalid, disabled), the operator-facing reasons behind it, the active V2 artifact
    and its activation health. Read-only: it never compiles, activates or writes anything.

     Report every installed playbook's V1->V2 migration readiness: its disposition (ready,
    question_required, invalid, disabled), the operator-facing reasons behind it, the active V2 artifact
    and its activation health. Read-only: it never compiles, activates or writes anything.

    Args:
        body (PlaybookMigrationInventoryRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookMigrationInventoryResponse | PlaybookMigrationInventoryResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookMigrationInventoryRequest,
) -> PlaybookMigrationInventoryResponse | PlaybookMigrationInventoryResponse422 | None:
    """Report every installed playbook's V1->V2 migration readiness: its disposition (ready,
    question_required, invalid, disabled), the operator-facing reasons behind it, the active V2 artifact
    and its activation health. Read-only: it never compiles, activates or writes anything.

     Report every installed playbook's V1->V2 migration readiness: its disposition (ready,
    question_required, invalid, disabled), the operator-facing reasons behind it, the active V2 artifact
    and its activation health. Read-only: it never compiles, activates or writes anything.

    Args:
        body (PlaybookMigrationInventoryRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookMigrationInventoryResponse | PlaybookMigrationInventoryResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
