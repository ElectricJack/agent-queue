from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.knowledge_authority_revoke_request import KnowledgeAuthorityRevokeRequest
from ...models.knowledge_authority_revoke_response_422 import KnowledgeAuthorityRevokeResponse422
from ...models.knowledge_protection_response import KnowledgeProtectionResponse
from ...types import Response


def _get_kwargs(
    *,
    body: KnowledgeAuthorityRevokeRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/knowledge/authority-revoke",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> KnowledgeAuthorityRevokeResponse422 | KnowledgeProtectionResponse | None:
    if response.status_code == 200:
        response_200 = KnowledgeProtectionResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = KnowledgeAuthorityRevokeResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[KnowledgeAuthorityRevokeResponse422 | KnowledgeProtectionResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: KnowledgeAuthorityRevokeRequest,
) -> Response[KnowledgeAuthorityRevokeResponse422 | KnowledgeProtectionResponse]:
    """Revoke policy authority without rewriting content history.

     Revoke policy authority without rewriting content history.

    Args:
        body (KnowledgeAuthorityRevokeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[KnowledgeAuthorityRevokeResponse422 | KnowledgeProtectionResponse]
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
    body: KnowledgeAuthorityRevokeRequest,
) -> KnowledgeAuthorityRevokeResponse422 | KnowledgeProtectionResponse | None:
    """Revoke policy authority without rewriting content history.

     Revoke policy authority without rewriting content history.

    Args:
        body (KnowledgeAuthorityRevokeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        KnowledgeAuthorityRevokeResponse422 | KnowledgeProtectionResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: KnowledgeAuthorityRevokeRequest,
) -> Response[KnowledgeAuthorityRevokeResponse422 | KnowledgeProtectionResponse]:
    """Revoke policy authority without rewriting content history.

     Revoke policy authority without rewriting content history.

    Args:
        body (KnowledgeAuthorityRevokeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[KnowledgeAuthorityRevokeResponse422 | KnowledgeProtectionResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: KnowledgeAuthorityRevokeRequest,
) -> KnowledgeAuthorityRevokeResponse422 | KnowledgeProtectionResponse | None:
    """Revoke policy authority without rewriting content history.

     Revoke policy authority without rewriting content history.

    Args:
        body (KnowledgeAuthorityRevokeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        KnowledgeAuthorityRevokeResponse422 | KnowledgeProtectionResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
