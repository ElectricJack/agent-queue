from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.delete_playbook_request import DeletePlaybookRequest
from ...models.delete_playbook_response import DeletePlaybookResponse
from ...models.delete_playbook_response_422 import DeletePlaybookResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: DeletePlaybookRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/delete",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> DeletePlaybookResponse | DeletePlaybookResponse422 | None:
    if response.status_code == 200:
        response_200 = DeletePlaybookResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = DeletePlaybookResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[DeletePlaybookResponse | DeletePlaybookResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DeletePlaybookRequest,
) -> Response[DeletePlaybookResponse | DeletePlaybookResponse422]:
    """Archive a playbook's source file to vault/trash/playbooks/ and remove it from the active registry.
    Historical playbook_runs rows are preserved.

     Archive a playbook's source file to vault/trash/playbooks/ and remove it from the active registry.
    Historical playbook_runs rows are preserved.

    Args:
        body (DeletePlaybookRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DeletePlaybookResponse | DeletePlaybookResponse422]
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
    body: DeletePlaybookRequest,
) -> DeletePlaybookResponse | DeletePlaybookResponse422 | None:
    """Archive a playbook's source file to vault/trash/playbooks/ and remove it from the active registry.
    Historical playbook_runs rows are preserved.

     Archive a playbook's source file to vault/trash/playbooks/ and remove it from the active registry.
    Historical playbook_runs rows are preserved.

    Args:
        body (DeletePlaybookRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DeletePlaybookResponse | DeletePlaybookResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DeletePlaybookRequest,
) -> Response[DeletePlaybookResponse | DeletePlaybookResponse422]:
    """Archive a playbook's source file to vault/trash/playbooks/ and remove it from the active registry.
    Historical playbook_runs rows are preserved.

     Archive a playbook's source file to vault/trash/playbooks/ and remove it from the active registry.
    Historical playbook_runs rows are preserved.

    Args:
        body (DeletePlaybookRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DeletePlaybookResponse | DeletePlaybookResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: DeletePlaybookRequest,
) -> DeletePlaybookResponse | DeletePlaybookResponse422 | None:
    """Archive a playbook's source file to vault/trash/playbooks/ and remove it from the active registry.
    Historical playbook_runs rows are preserved.

     Archive a playbook's source file to vault/trash/playbooks/ and remove it from the active registry.
    Historical playbook_runs rows are preserved.

    Args:
        body (DeletePlaybookRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DeletePlaybookResponse | DeletePlaybookResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
