from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.read_project_memory_file_request import ReadProjectMemoryFileRequest
from ...models.read_project_memory_file_response import ReadProjectMemoryFileResponse
from ...models.read_project_memory_file_response_422 import ReadProjectMemoryFileResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ReadProjectMemoryFileRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/files/read-project-memory",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ReadProjectMemoryFileResponse | ReadProjectMemoryFileResponse422 | None:
    if response.status_code == 200:
        response_200 = ReadProjectMemoryFileResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ReadProjectMemoryFileResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ReadProjectMemoryFileResponse | ReadProjectMemoryFileResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ReadProjectMemoryFileRequest,
) -> Response[ReadProjectMemoryFileResponse | ReadProjectMemoryFileResponse422]:
    """Read a markdown file from a project's system memory vault. Resolves paths under
    ``{data_dir}/vault/projects/<project_id>/memory/<path>``, which lives outside the regular workspace
    sandbox. Use this for reading consolidation markers, insight files, or knowledge entries from the
    agent-queue vault. Path traversal outside the project's memory directory is rejected. Returns
    ``{missing: true, error}`` when the file does not exist so the memory-consolidation playbook can
    treat it as ``last_consolidated: null``.

     Read a markdown file from a project's system memory vault. Resolves paths under
    ``{data_dir}/vault/projects/<project_id>/memory/<path>``, which lives outside the regular workspace
    sandbox. Use this for reading consolidation markers, insight files, or knowledge entries from the
    agent-queue vault. Path traversal outside the project's memory directory is rejected. Returns
    ``{missing: true, error}`` when the file does not exist so the memory-consolidation playbook can
    treat it as ``last_consolidated: null``.

    Args:
        body (ReadProjectMemoryFileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ReadProjectMemoryFileResponse | ReadProjectMemoryFileResponse422]
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
    body: ReadProjectMemoryFileRequest,
) -> ReadProjectMemoryFileResponse | ReadProjectMemoryFileResponse422 | None:
    """Read a markdown file from a project's system memory vault. Resolves paths under
    ``{data_dir}/vault/projects/<project_id>/memory/<path>``, which lives outside the regular workspace
    sandbox. Use this for reading consolidation markers, insight files, or knowledge entries from the
    agent-queue vault. Path traversal outside the project's memory directory is rejected. Returns
    ``{missing: true, error}`` when the file does not exist so the memory-consolidation playbook can
    treat it as ``last_consolidated: null``.

     Read a markdown file from a project's system memory vault. Resolves paths under
    ``{data_dir}/vault/projects/<project_id>/memory/<path>``, which lives outside the regular workspace
    sandbox. Use this for reading consolidation markers, insight files, or knowledge entries from the
    agent-queue vault. Path traversal outside the project's memory directory is rejected. Returns
    ``{missing: true, error}`` when the file does not exist so the memory-consolidation playbook can
    treat it as ``last_consolidated: null``.

    Args:
        body (ReadProjectMemoryFileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ReadProjectMemoryFileResponse | ReadProjectMemoryFileResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ReadProjectMemoryFileRequest,
) -> Response[ReadProjectMemoryFileResponse | ReadProjectMemoryFileResponse422]:
    """Read a markdown file from a project's system memory vault. Resolves paths under
    ``{data_dir}/vault/projects/<project_id>/memory/<path>``, which lives outside the regular workspace
    sandbox. Use this for reading consolidation markers, insight files, or knowledge entries from the
    agent-queue vault. Path traversal outside the project's memory directory is rejected. Returns
    ``{missing: true, error}`` when the file does not exist so the memory-consolidation playbook can
    treat it as ``last_consolidated: null``.

     Read a markdown file from a project's system memory vault. Resolves paths under
    ``{data_dir}/vault/projects/<project_id>/memory/<path>``, which lives outside the regular workspace
    sandbox. Use this for reading consolidation markers, insight files, or knowledge entries from the
    agent-queue vault. Path traversal outside the project's memory directory is rejected. Returns
    ``{missing: true, error}`` when the file does not exist so the memory-consolidation playbook can
    treat it as ``last_consolidated: null``.

    Args:
        body (ReadProjectMemoryFileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ReadProjectMemoryFileResponse | ReadProjectMemoryFileResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ReadProjectMemoryFileRequest,
) -> ReadProjectMemoryFileResponse | ReadProjectMemoryFileResponse422 | None:
    """Read a markdown file from a project's system memory vault. Resolves paths under
    ``{data_dir}/vault/projects/<project_id>/memory/<path>``, which lives outside the regular workspace
    sandbox. Use this for reading consolidation markers, insight files, or knowledge entries from the
    agent-queue vault. Path traversal outside the project's memory directory is rejected. Returns
    ``{missing: true, error}`` when the file does not exist so the memory-consolidation playbook can
    treat it as ``last_consolidated: null``.

     Read a markdown file from a project's system memory vault. Resolves paths under
    ``{data_dir}/vault/projects/<project_id>/memory/<path>``, which lives outside the regular workspace
    sandbox. Use this for reading consolidation markers, insight files, or knowledge entries from the
    agent-queue vault. Path traversal outside the project's memory directory is rejected. Returns
    ``{missing: true, error}`` when the file does not exist so the memory-consolidation playbook can
    treat it as ``last_consolidated: null``.

    Args:
        body (ReadProjectMemoryFileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ReadProjectMemoryFileResponse | ReadProjectMemoryFileResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
