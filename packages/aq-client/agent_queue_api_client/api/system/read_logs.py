from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.read_logs_request import ReadLogsRequest
from ...models.read_logs_response import ReadLogsResponse
from ...models.read_logs_response_422 import ReadLogsResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ReadLogsRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/read-logs",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ReadLogsResponse | ReadLogsResponse422 | None:
    if response.status_code == 200:
        response_200 = ReadLogsResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ReadLogsResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ReadLogsResponse | ReadLogsResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ReadLogsRequest,
) -> Response[ReadLogsResponse | ReadLogsResponse422]:
    """Read and filter the daemon's structured JSONL log file. Returns parsed log entries with severity,
    timestamps, and context fields. Use this to inspect detailed system behavior, diagnose errors, or
    analyze component-level activity.

     Read and filter the daemon's structured JSONL log file. Returns parsed log entries with severity,
    timestamps, and context fields. Use this to inspect detailed system behavior, diagnose errors, or
    analyze component-level activity.

    Args:
        body (ReadLogsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ReadLogsResponse | ReadLogsResponse422]
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
    body: ReadLogsRequest,
) -> ReadLogsResponse | ReadLogsResponse422 | None:
    """Read and filter the daemon's structured JSONL log file. Returns parsed log entries with severity,
    timestamps, and context fields. Use this to inspect detailed system behavior, diagnose errors, or
    analyze component-level activity.

     Read and filter the daemon's structured JSONL log file. Returns parsed log entries with severity,
    timestamps, and context fields. Use this to inspect detailed system behavior, diagnose errors, or
    analyze component-level activity.

    Args:
        body (ReadLogsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ReadLogsResponse | ReadLogsResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ReadLogsRequest,
) -> Response[ReadLogsResponse | ReadLogsResponse422]:
    """Read and filter the daemon's structured JSONL log file. Returns parsed log entries with severity,
    timestamps, and context fields. Use this to inspect detailed system behavior, diagnose errors, or
    analyze component-level activity.

     Read and filter the daemon's structured JSONL log file. Returns parsed log entries with severity,
    timestamps, and context fields. Use this to inspect detailed system behavior, diagnose errors, or
    analyze component-level activity.

    Args:
        body (ReadLogsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ReadLogsResponse | ReadLogsResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ReadLogsRequest,
) -> ReadLogsResponse | ReadLogsResponse422 | None:
    """Read and filter the daemon's structured JSONL log file. Returns parsed log entries with severity,
    timestamps, and context fields. Use this to inspect detailed system behavior, diagnose errors, or
    analyze component-level activity.

     Read and filter the daemon's structured JSONL log file. Returns parsed log entries with severity,
    timestamps, and context fields. Use this to inspect detailed system behavior, diagnose errors, or
    analyze component-level activity.

    Args:
        body (ReadLogsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ReadLogsResponse | ReadLogsResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
