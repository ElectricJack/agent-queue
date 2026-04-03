from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.import_profile_request import ImportProfileRequest
from ...models.import_profile_response import ImportProfileResponse
from ...models.import_profile_response_422 import ImportProfileResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ImportProfileRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/agent/import-profile",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ImportProfileResponse | ImportProfileResponse422 | None:
    if response.status_code == 200:
        response_200 = ImportProfileResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ImportProfileResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ImportProfileResponse | ImportProfileResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ImportProfileRequest,
) -> Response[ImportProfileResponse | ImportProfileResponse422]:
    """Import an agent profile from YAML text or a GitHub gist URL. If the profile has an install manifest,
    dependencies are auto-installed.

     Import an agent profile from YAML text or a GitHub gist URL. If the profile has an install manifest,
    dependencies are auto-installed.

    Args:
        body (ImportProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ImportProfileResponse | ImportProfileResponse422]
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
    body: ImportProfileRequest,
) -> ImportProfileResponse | ImportProfileResponse422 | None:
    """Import an agent profile from YAML text or a GitHub gist URL. If the profile has an install manifest,
    dependencies are auto-installed.

     Import an agent profile from YAML text or a GitHub gist URL. If the profile has an install manifest,
    dependencies are auto-installed.

    Args:
        body (ImportProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ImportProfileResponse | ImportProfileResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ImportProfileRequest,
) -> Response[ImportProfileResponse | ImportProfileResponse422]:
    """Import an agent profile from YAML text or a GitHub gist URL. If the profile has an install manifest,
    dependencies are auto-installed.

     Import an agent profile from YAML text or a GitHub gist URL. If the profile has an install manifest,
    dependencies are auto-installed.

    Args:
        body (ImportProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ImportProfileResponse | ImportProfileResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ImportProfileRequest,
) -> ImportProfileResponse | ImportProfileResponse422 | None:
    """Import an agent profile from YAML text or a GitHub gist URL. If the profile has an install manifest,
    dependencies are auto-installed.

     Import an agent profile from YAML text or a GitHub gist URL. If the profile has an install manifest,
    dependencies are auto-installed.

    Args:
        body (ImportProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ImportProfileResponse | ImportProfileResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
