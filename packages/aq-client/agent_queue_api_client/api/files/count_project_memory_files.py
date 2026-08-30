from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.count_project_memory_files_request import CountProjectMemoryFilesRequest
from ...models.count_project_memory_files_response import CountProjectMemoryFilesResponse
from ...models.count_project_memory_files_response_422 import CountProjectMemoryFilesResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: CountProjectMemoryFilesRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/files/count-project-memory",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> CountProjectMemoryFilesResponse | CountProjectMemoryFilesResponse422 | None:
    if response.status_code == 200:
        response_200 = CountProjectMemoryFilesResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = CountProjectMemoryFilesResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[CountProjectMemoryFilesResponse | CountProjectMemoryFilesResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CountProjectMemoryFilesRequest,
) -> Response[CountProjectMemoryFilesResponse | CountProjectMemoryFilesResponse422]:
    """Count files in a subdirectory of a project's system memory vault
    (``{data_dir}/vault/projects/<project_id>/memory/<path>``). Optionally filter by modification time
    via ``newer_than`` (ISO 8601 timestamp). Returns ``{count, total, missing?}``; a missing directory
    is reported as ``count: 0, missing: true`` rather than an error so the memory-consolidation playbook
    can handle first-run projects cleanly.

     Count files in a subdirectory of a project's system memory vault
    (``{data_dir}/vault/projects/<project_id>/memory/<path>``). Optionally filter by modification time
    via ``newer_than`` (ISO 8601 timestamp). Returns ``{count, total, missing?}``; a missing directory
    is reported as ``count: 0, missing: true`` rather than an error so the memory-consolidation playbook
    can handle first-run projects cleanly.

    Args:
        body (CountProjectMemoryFilesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CountProjectMemoryFilesResponse | CountProjectMemoryFilesResponse422]
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
    body: CountProjectMemoryFilesRequest,
) -> CountProjectMemoryFilesResponse | CountProjectMemoryFilesResponse422 | None:
    """Count files in a subdirectory of a project's system memory vault
    (``{data_dir}/vault/projects/<project_id>/memory/<path>``). Optionally filter by modification time
    via ``newer_than`` (ISO 8601 timestamp). Returns ``{count, total, missing?}``; a missing directory
    is reported as ``count: 0, missing: true`` rather than an error so the memory-consolidation playbook
    can handle first-run projects cleanly.

     Count files in a subdirectory of a project's system memory vault
    (``{data_dir}/vault/projects/<project_id>/memory/<path>``). Optionally filter by modification time
    via ``newer_than`` (ISO 8601 timestamp). Returns ``{count, total, missing?}``; a missing directory
    is reported as ``count: 0, missing: true`` rather than an error so the memory-consolidation playbook
    can handle first-run projects cleanly.

    Args:
        body (CountProjectMemoryFilesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CountProjectMemoryFilesResponse | CountProjectMemoryFilesResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CountProjectMemoryFilesRequest,
) -> Response[CountProjectMemoryFilesResponse | CountProjectMemoryFilesResponse422]:
    """Count files in a subdirectory of a project's system memory vault
    (``{data_dir}/vault/projects/<project_id>/memory/<path>``). Optionally filter by modification time
    via ``newer_than`` (ISO 8601 timestamp). Returns ``{count, total, missing?}``; a missing directory
    is reported as ``count: 0, missing: true`` rather than an error so the memory-consolidation playbook
    can handle first-run projects cleanly.

     Count files in a subdirectory of a project's system memory vault
    (``{data_dir}/vault/projects/<project_id>/memory/<path>``). Optionally filter by modification time
    via ``newer_than`` (ISO 8601 timestamp). Returns ``{count, total, missing?}``; a missing directory
    is reported as ``count: 0, missing: true`` rather than an error so the memory-consolidation playbook
    can handle first-run projects cleanly.

    Args:
        body (CountProjectMemoryFilesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CountProjectMemoryFilesResponse | CountProjectMemoryFilesResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: CountProjectMemoryFilesRequest,
) -> CountProjectMemoryFilesResponse | CountProjectMemoryFilesResponse422 | None:
    """Count files in a subdirectory of a project's system memory vault
    (``{data_dir}/vault/projects/<project_id>/memory/<path>``). Optionally filter by modification time
    via ``newer_than`` (ISO 8601 timestamp). Returns ``{count, total, missing?}``; a missing directory
    is reported as ``count: 0, missing: true`` rather than an error so the memory-consolidation playbook
    can handle first-run projects cleanly.

     Count files in a subdirectory of a project's system memory vault
    (``{data_dir}/vault/projects/<project_id>/memory/<path>``). Optionally filter by modification time
    via ``newer_than`` (ISO 8601 timestamp). Returns ``{count, total, missing?}``; a missing directory
    is reported as ``count: 0, missing: true`` rather than an error so the memory-consolidation playbook
    can handle first-run projects cleanly.

    Args:
        body (CountProjectMemoryFilesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CountProjectMemoryFilesResponse | CountProjectMemoryFilesResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
