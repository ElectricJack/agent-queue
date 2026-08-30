from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.show_effective_profile_request import ShowEffectiveProfileRequest
from ...models.show_effective_profile_response import ShowEffectiveProfileResponse
from ...models.show_effective_profile_response_422 import ShowEffectiveProfileResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ShowEffectiveProfileRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/agent/show-effective-profile",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ShowEffectiveProfileResponse | ShowEffectiveProfileResponse422 | None:
    if response.status_code == 200:
        response_200 = ShowEffectiveProfileResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ShowEffectiveProfileResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ShowEffectiveProfileResponse | ShowEffectiveProfileResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ShowEffectiveProfileRequest,
) -> Response[ShowEffectiveProfileResponse | ShowEffectiveProfileResponse422]:
    """Run the orchestrator's profile resolution cascade for a (project_id, agent_type) pair and return the
    merged profile the next task launch would use.  Debug helper.

     Run the orchestrator's profile resolution cascade for a (project_id, agent_type) pair and return the
    merged profile the next task launch would use.  Debug helper.

    Args:
        body (ShowEffectiveProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ShowEffectiveProfileResponse | ShowEffectiveProfileResponse422]
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
    body: ShowEffectiveProfileRequest,
) -> ShowEffectiveProfileResponse | ShowEffectiveProfileResponse422 | None:
    """Run the orchestrator's profile resolution cascade for a (project_id, agent_type) pair and return the
    merged profile the next task launch would use.  Debug helper.

     Run the orchestrator's profile resolution cascade for a (project_id, agent_type) pair and return the
    merged profile the next task launch would use.  Debug helper.

    Args:
        body (ShowEffectiveProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ShowEffectiveProfileResponse | ShowEffectiveProfileResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ShowEffectiveProfileRequest,
) -> Response[ShowEffectiveProfileResponse | ShowEffectiveProfileResponse422]:
    """Run the orchestrator's profile resolution cascade for a (project_id, agent_type) pair and return the
    merged profile the next task launch would use.  Debug helper.

     Run the orchestrator's profile resolution cascade for a (project_id, agent_type) pair and return the
    merged profile the next task launch would use.  Debug helper.

    Args:
        body (ShowEffectiveProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ShowEffectiveProfileResponse | ShowEffectiveProfileResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ShowEffectiveProfileRequest,
) -> ShowEffectiveProfileResponse | ShowEffectiveProfileResponse422 | None:
    """Run the orchestrator's profile resolution cascade for a (project_id, agent_type) pair and return the
    merged profile the next task launch would use.  Debug helper.

     Run the orchestrator's profile resolution cascade for a (project_id, agent_type) pair and return the
    merged profile the next task launch would use.  Debug helper.

    Args:
        body (ShowEffectiveProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ShowEffectiveProfileResponse | ShowEffectiveProfileResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
