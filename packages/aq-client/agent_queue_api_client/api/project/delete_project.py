from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.delete_project_request import DeleteProjectRequest
from ...models.delete_project_response import DeleteProjectResponse
from ...models.delete_project_response_422 import DeleteProjectResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: DeleteProjectRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/project/delete",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> DeleteProjectResponse | DeleteProjectResponse422 | None:
    if response.status_code == 200:
        response_200 = DeleteProjectResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = DeleteProjectResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[DeleteProjectResponse | DeleteProjectResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DeleteProjectRequest,
) -> Response[DeleteProjectResponse | DeleteProjectResponse422]:
    """Delete a project and all associated data (tasks, repos, results, token ledger). Cannot delete if any
    task is IN_PROGRESS. In-memory channel caches are automatically purged. Optionally archive the
    project's Discord channels.

     Delete a project and all associated data (tasks, repos, results, token ledger). Cannot delete if any
    task is IN_PROGRESS. In-memory channel caches are automatically purged. Optionally archive the
    project's Discord channels.

    Args:
        body (DeleteProjectRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DeleteProjectResponse | DeleteProjectResponse422]
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
    body: DeleteProjectRequest,
) -> DeleteProjectResponse | DeleteProjectResponse422 | None:
    """Delete a project and all associated data (tasks, repos, results, token ledger). Cannot delete if any
    task is IN_PROGRESS. In-memory channel caches are automatically purged. Optionally archive the
    project's Discord channels.

     Delete a project and all associated data (tasks, repos, results, token ledger). Cannot delete if any
    task is IN_PROGRESS. In-memory channel caches are automatically purged. Optionally archive the
    project's Discord channels.

    Args:
        body (DeleteProjectRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DeleteProjectResponse | DeleteProjectResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DeleteProjectRequest,
) -> Response[DeleteProjectResponse | DeleteProjectResponse422]:
    """Delete a project and all associated data (tasks, repos, results, token ledger). Cannot delete if any
    task is IN_PROGRESS. In-memory channel caches are automatically purged. Optionally archive the
    project's Discord channels.

     Delete a project and all associated data (tasks, repos, results, token ledger). Cannot delete if any
    task is IN_PROGRESS. In-memory channel caches are automatically purged. Optionally archive the
    project's Discord channels.

    Args:
        body (DeleteProjectRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DeleteProjectResponse | DeleteProjectResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: DeleteProjectRequest,
) -> DeleteProjectResponse | DeleteProjectResponse422 | None:
    """Delete a project and all associated data (tasks, repos, results, token ledger). Cannot delete if any
    task is IN_PROGRESS. In-memory channel caches are automatically purged. Optionally archive the
    project's Discord channels.

     Delete a project and all associated data (tasks, repos, results, token ledger). Cannot delete if any
    task is IN_PROGRESS. In-memory channel caches are automatically purged. Optionally archive the
    project's Discord channels.

    Args:
        body (DeleteProjectRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DeleteProjectResponse | DeleteProjectResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
