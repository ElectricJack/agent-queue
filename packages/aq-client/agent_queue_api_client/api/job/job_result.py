from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.job_error_response import JobErrorResponse
from ...models.job_result_args import JobResultArgs
from ...models.job_result_response import JobResultResponse
from ...types import Response


def _get_kwargs(
    *,
    body: JobResultArgs,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/job/result",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> JobErrorResponse | JobResultResponse | None:
    if response.status_code == 200:
        response_200 = JobResultResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = JobErrorResponse.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[JobErrorResponse | JobResultResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: JobResultArgs,
) -> Response[JobErrorResponse | JobResultResponse]:
    """Read a job's immutable result and bounded excerpt.

     Read a job's immutable result and bounded excerpt.

    Args:
        body (JobResultArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[JobErrorResponse | JobResultResponse]
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
    body: JobResultArgs,
) -> JobErrorResponse | JobResultResponse | None:
    """Read a job's immutable result and bounded excerpt.

     Read a job's immutable result and bounded excerpt.

    Args:
        body (JobResultArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        JobErrorResponse | JobResultResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: JobResultArgs,
) -> Response[JobErrorResponse | JobResultResponse]:
    """Read a job's immutable result and bounded excerpt.

     Read a job's immutable result and bounded excerpt.

    Args:
        body (JobResultArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[JobErrorResponse | JobResultResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: JobResultArgs,
) -> JobErrorResponse | JobResultResponse | None:
    """Read a job's immutable result and bounded excerpt.

     Read a job's immutable result and bounded excerpt.

    Args:
        body (JobResultArgs):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        JobErrorResponse | JobResultResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
