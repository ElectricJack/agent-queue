from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.knowledge_lifecycle_response import KnowledgeLifecycleResponse
from ...models.knowledge_restore_request import KnowledgeRestoreRequest
from ...models.knowledge_restore_response_422 import KnowledgeRestoreResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: KnowledgeRestoreRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/knowledge/restore",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> KnowledgeLifecycleResponse | KnowledgeRestoreResponse422 | None:
    if response.status_code == 200:
        response_200 = KnowledgeLifecycleResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = KnowledgeRestoreResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[KnowledgeLifecycleResponse | KnowledgeRestoreResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: KnowledgeRestoreRequest,
) -> Response[KnowledgeLifecycleResponse | KnowledgeRestoreResponse422]:
    """Restore a knowledge finding to a retained revision. Supervisor-only. Idempotent by idempotency_key.

     Restore a knowledge finding to a retained revision. Supervisor-only. Idempotent by idempotency_key.

    Args:
        body (KnowledgeRestoreRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[KnowledgeLifecycleResponse | KnowledgeRestoreResponse422]
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
    body: KnowledgeRestoreRequest,
) -> KnowledgeLifecycleResponse | KnowledgeRestoreResponse422 | None:
    """Restore a knowledge finding to a retained revision. Supervisor-only. Idempotent by idempotency_key.

     Restore a knowledge finding to a retained revision. Supervisor-only. Idempotent by idempotency_key.

    Args:
        body (KnowledgeRestoreRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        KnowledgeLifecycleResponse | KnowledgeRestoreResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: KnowledgeRestoreRequest,
) -> Response[KnowledgeLifecycleResponse | KnowledgeRestoreResponse422]:
    """Restore a knowledge finding to a retained revision. Supervisor-only. Idempotent by idempotency_key.

     Restore a knowledge finding to a retained revision. Supervisor-only. Idempotent by idempotency_key.

    Args:
        body (KnowledgeRestoreRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[KnowledgeLifecycleResponse | KnowledgeRestoreResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: KnowledgeRestoreRequest,
) -> KnowledgeLifecycleResponse | KnowledgeRestoreResponse422 | None:
    """Restore a knowledge finding to a retained revision. Supervisor-only. Idempotent by idempotency_key.

     Restore a knowledge finding to a retained revision. Supervisor-only. Idempotent by idempotency_key.

    Args:
        body (KnowledgeRestoreRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        KnowledgeLifecycleResponse | KnowledgeRestoreResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
