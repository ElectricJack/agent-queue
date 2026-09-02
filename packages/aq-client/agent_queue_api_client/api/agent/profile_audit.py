from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.profile_audit_request import ProfileAuditRequest
from ...models.profile_audit_response import ProfileAuditResponse
from ...models.profile_audit_response_422 import ProfileAuditResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ProfileAuditRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/agent/profile-audit",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ProfileAuditResponse | ProfileAuditResponse422 | None:
    if response.status_code == 200:
        response_200 = ProfileAuditResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ProfileAuditResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ProfileAuditResponse | ProfileAuditResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProfileAuditRequest,
) -> Response[ProfileAuditResponse | ProfileAuditResponse422]:
    """Report which agent profiles still derive their capabilities from the legacy allowed_tools list
    rather than an explicit ## Capabilities block. One row per profile with its source
    (explicit/legacy), the three capability namespaces, and the policy fingerprint — the migration list
    to clear before capability enforcement is set to 'enforce'.

     Report which agent profiles still derive their capabilities from the legacy allowed_tools list
    rather than an explicit ## Capabilities block. One row per profile with its source
    (explicit/legacy), the three capability namespaces, and the policy fingerprint — the migration list
    to clear before capability enforcement is set to 'enforce'.

    Args:
        body (ProfileAuditRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProfileAuditResponse | ProfileAuditResponse422]
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
    body: ProfileAuditRequest,
) -> ProfileAuditResponse | ProfileAuditResponse422 | None:
    """Report which agent profiles still derive their capabilities from the legacy allowed_tools list
    rather than an explicit ## Capabilities block. One row per profile with its source
    (explicit/legacy), the three capability namespaces, and the policy fingerprint — the migration list
    to clear before capability enforcement is set to 'enforce'.

     Report which agent profiles still derive their capabilities from the legacy allowed_tools list
    rather than an explicit ## Capabilities block. One row per profile with its source
    (explicit/legacy), the three capability namespaces, and the policy fingerprint — the migration list
    to clear before capability enforcement is set to 'enforce'.

    Args:
        body (ProfileAuditRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProfileAuditResponse | ProfileAuditResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProfileAuditRequest,
) -> Response[ProfileAuditResponse | ProfileAuditResponse422]:
    """Report which agent profiles still derive their capabilities from the legacy allowed_tools list
    rather than an explicit ## Capabilities block. One row per profile with its source
    (explicit/legacy), the three capability namespaces, and the policy fingerprint — the migration list
    to clear before capability enforcement is set to 'enforce'.

     Report which agent profiles still derive their capabilities from the legacy allowed_tools list
    rather than an explicit ## Capabilities block. One row per profile with its source
    (explicit/legacy), the three capability namespaces, and the policy fingerprint — the migration list
    to clear before capability enforcement is set to 'enforce'.

    Args:
        body (ProfileAuditRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProfileAuditResponse | ProfileAuditResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ProfileAuditRequest,
) -> ProfileAuditResponse | ProfileAuditResponse422 | None:
    """Report which agent profiles still derive their capabilities from the legacy allowed_tools list
    rather than an explicit ## Capabilities block. One row per profile with its source
    (explicit/legacy), the three capability namespaces, and the policy fingerprint — the migration list
    to clear before capability enforcement is set to 'enforce'.

     Report which agent profiles still derive their capabilities from the legacy allowed_tools list
    rather than an explicit ## Capabilities block. One row per profile with its source
    (explicit/legacy), the three capability namespaces, and the policy fingerprint — the migration list
    to clear before capability enforcement is set to 'enforce'.

    Args:
        body (ProfileAuditRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProfileAuditResponse | ProfileAuditResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
