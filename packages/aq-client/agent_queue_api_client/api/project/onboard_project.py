from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.onboard_project_request import OnboardProjectRequest
from ...models.onboard_project_response import OnboardProjectResponse
from ...models.onboard_project_response_422 import OnboardProjectResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: OnboardProjectRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/project/onboard",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> OnboardProjectResponse | OnboardProjectResponse422 | None:
    if response.status_code == 200:
        response_200 = OnboardProjectResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = OnboardProjectResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[OnboardProjectResponse | OnboardProjectResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: OnboardProjectRequest,
) -> Response[OnboardProjectResponse | OnboardProjectResponse422]:
    """Onboard a project from the dashboard: link an existing local repository, initialise a new one
    (optionally creating a GitHub remote and README commit), or clone a GitHub repository, always
    beneath a configured project root. Creates the project, its primary project-repo workspace and vault
    structure in one server-owned saga. `request_id` is a durable idempotency key. Mode-specific fields
    are only valid for their `source_mode`.

     Onboard a project from the dashboard: link an existing local repository, initialise a new one
    (optionally creating a GitHub remote and README commit), or clone a GitHub repository, always
    beneath a configured project root. Creates the project, its primary project-repo workspace and vault
    structure in one server-owned saga. `request_id` is a durable idempotency key. Mode-specific fields
    are only valid for their `source_mode`.

    Args:
        body (OnboardProjectRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[OnboardProjectResponse | OnboardProjectResponse422]
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
    body: OnboardProjectRequest,
) -> OnboardProjectResponse | OnboardProjectResponse422 | None:
    """Onboard a project from the dashboard: link an existing local repository, initialise a new one
    (optionally creating a GitHub remote and README commit), or clone a GitHub repository, always
    beneath a configured project root. Creates the project, its primary project-repo workspace and vault
    structure in one server-owned saga. `request_id` is a durable idempotency key. Mode-specific fields
    are only valid for their `source_mode`.

     Onboard a project from the dashboard: link an existing local repository, initialise a new one
    (optionally creating a GitHub remote and README commit), or clone a GitHub repository, always
    beneath a configured project root. Creates the project, its primary project-repo workspace and vault
    structure in one server-owned saga. `request_id` is a durable idempotency key. Mode-specific fields
    are only valid for their `source_mode`.

    Args:
        body (OnboardProjectRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        OnboardProjectResponse | OnboardProjectResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: OnboardProjectRequest,
) -> Response[OnboardProjectResponse | OnboardProjectResponse422]:
    """Onboard a project from the dashboard: link an existing local repository, initialise a new one
    (optionally creating a GitHub remote and README commit), or clone a GitHub repository, always
    beneath a configured project root. Creates the project, its primary project-repo workspace and vault
    structure in one server-owned saga. `request_id` is a durable idempotency key. Mode-specific fields
    are only valid for their `source_mode`.

     Onboard a project from the dashboard: link an existing local repository, initialise a new one
    (optionally creating a GitHub remote and README commit), or clone a GitHub repository, always
    beneath a configured project root. Creates the project, its primary project-repo workspace and vault
    structure in one server-owned saga. `request_id` is a durable idempotency key. Mode-specific fields
    are only valid for their `source_mode`.

    Args:
        body (OnboardProjectRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[OnboardProjectResponse | OnboardProjectResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: OnboardProjectRequest,
) -> OnboardProjectResponse | OnboardProjectResponse422 | None:
    """Onboard a project from the dashboard: link an existing local repository, initialise a new one
    (optionally creating a GitHub remote and README commit), or clone a GitHub repository, always
    beneath a configured project root. Creates the project, its primary project-repo workspace and vault
    structure in one server-owned saga. `request_id` is a durable idempotency key. Mode-specific fields
    are only valid for their `source_mode`.

     Onboard a project from the dashboard: link an existing local repository, initialise a new one
    (optionally creating a GitHub remote and README commit), or clone a GitHub repository, always
    beneath a configured project root. Creates the project, its primary project-repo workspace and vault
    structure in one server-owned saga. `request_id` is a durable idempotency key. Mode-specific fields
    are only valid for their `source_mode`.

    Args:
        body (OnboardProjectRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        OnboardProjectResponse | OnboardProjectResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
