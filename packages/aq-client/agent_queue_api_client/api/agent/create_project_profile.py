from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.create_project_profile_request import CreateProjectProfileRequest
from ...models.create_project_profile_response import CreateProjectProfileResponse
from ...models.create_project_profile_response_422 import CreateProjectProfileResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: CreateProjectProfileRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/agent/create-project-profile",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> CreateProjectProfileResponse | CreateProjectProfileResponse422 | None:
    if response.status_code == 200:
        response_200 = CreateProjectProfileResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = CreateProjectProfileResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[CreateProjectProfileResponse | CreateProjectProfileResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CreateProjectProfileRequest,
) -> Response[CreateProjectProfileResponse | CreateProjectProfileResponse422]:
    """Create a project-scoped agent profile.  Composes ``project:<project_id>:<agent_type>`` as the
    profile id and writes the vault markdown.  When ``seed_from_global`` is true (default), starts from
    the matching global ``<agent_type>`` profile so the override is a delta.

     Create a project-scoped agent profile.  Composes ``project:<project_id>:<agent_type>`` as the
    profile id and writes the vault markdown.  When ``seed_from_global`` is true (default), starts from
    the matching global ``<agent_type>`` profile so the override is a delta.

    Args:
        body (CreateProjectProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CreateProjectProfileResponse | CreateProjectProfileResponse422]
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
    body: CreateProjectProfileRequest,
) -> CreateProjectProfileResponse | CreateProjectProfileResponse422 | None:
    """Create a project-scoped agent profile.  Composes ``project:<project_id>:<agent_type>`` as the
    profile id and writes the vault markdown.  When ``seed_from_global`` is true (default), starts from
    the matching global ``<agent_type>`` profile so the override is a delta.

     Create a project-scoped agent profile.  Composes ``project:<project_id>:<agent_type>`` as the
    profile id and writes the vault markdown.  When ``seed_from_global`` is true (default), starts from
    the matching global ``<agent_type>`` profile so the override is a delta.

    Args:
        body (CreateProjectProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CreateProjectProfileResponse | CreateProjectProfileResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CreateProjectProfileRequest,
) -> Response[CreateProjectProfileResponse | CreateProjectProfileResponse422]:
    """Create a project-scoped agent profile.  Composes ``project:<project_id>:<agent_type>`` as the
    profile id and writes the vault markdown.  When ``seed_from_global`` is true (default), starts from
    the matching global ``<agent_type>`` profile so the override is a delta.

     Create a project-scoped agent profile.  Composes ``project:<project_id>:<agent_type>`` as the
    profile id and writes the vault markdown.  When ``seed_from_global`` is true (default), starts from
    the matching global ``<agent_type>`` profile so the override is a delta.

    Args:
        body (CreateProjectProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CreateProjectProfileResponse | CreateProjectProfileResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: CreateProjectProfileRequest,
) -> CreateProjectProfileResponse | CreateProjectProfileResponse422 | None:
    """Create a project-scoped agent profile.  Composes ``project:<project_id>:<agent_type>`` as the
    profile id and writes the vault markdown.  When ``seed_from_global`` is true (default), starts from
    the matching global ``<agent_type>`` profile so the override is a delta.

     Create a project-scoped agent profile.  Composes ``project:<project_id>:<agent_type>`` as the
    profile id and writes the vault markdown.  When ``seed_from_global`` is true (default), starts from
    the matching global ``<agent_type>`` profile so the override is a delta.

    Args:
        body (CreateProjectProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CreateProjectProfileResponse | CreateProjectProfileResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
