from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.get_benchmark_report_request import GetBenchmarkReportRequest
from ...models.get_benchmark_report_response import GetBenchmarkReportResponse
from ...models.get_benchmark_report_response_422 import GetBenchmarkReportResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: GetBenchmarkReportRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/get-benchmark-report",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> GetBenchmarkReportResponse | GetBenchmarkReportResponse422 | None:
    if response.status_code == 200:
        response_200 = GetBenchmarkReportResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = GetBenchmarkReportResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[GetBenchmarkReportResponse | GetBenchmarkReportResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetBenchmarkReportRequest,
) -> Response[GetBenchmarkReportResponse | GetBenchmarkReportResponse422]:
    """Export usage, costs, routing provenance and stage measurements for an explicit frozen benchmark
    cohort. Requires an operator or project supervisor; missing attribution and charges remain unknown.

     Export usage, costs, routing provenance and stage measurements for an explicit frozen benchmark
    cohort. Requires an operator or project supervisor; missing attribution and charges remain unknown.

    Args:
        body (GetBenchmarkReportRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetBenchmarkReportResponse | GetBenchmarkReportResponse422]
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
    body: GetBenchmarkReportRequest,
) -> GetBenchmarkReportResponse | GetBenchmarkReportResponse422 | None:
    """Export usage, costs, routing provenance and stage measurements for an explicit frozen benchmark
    cohort. Requires an operator or project supervisor; missing attribution and charges remain unknown.

     Export usage, costs, routing provenance and stage measurements for an explicit frozen benchmark
    cohort. Requires an operator or project supervisor; missing attribution and charges remain unknown.

    Args:
        body (GetBenchmarkReportRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetBenchmarkReportResponse | GetBenchmarkReportResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetBenchmarkReportRequest,
) -> Response[GetBenchmarkReportResponse | GetBenchmarkReportResponse422]:
    """Export usage, costs, routing provenance and stage measurements for an explicit frozen benchmark
    cohort. Requires an operator or project supervisor; missing attribution and charges remain unknown.

     Export usage, costs, routing provenance and stage measurements for an explicit frozen benchmark
    cohort. Requires an operator or project supervisor; missing attribution and charges remain unknown.

    Args:
        body (GetBenchmarkReportRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetBenchmarkReportResponse | GetBenchmarkReportResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: GetBenchmarkReportRequest,
) -> GetBenchmarkReportResponse | GetBenchmarkReportResponse422 | None:
    """Export usage, costs, routing provenance and stage measurements for an explicit frozen benchmark
    cohort. Requires an operator or project supervisor; missing attribution and charges remain unknown.

     Export usage, costs, routing provenance and stage measurements for an explicit frozen benchmark
    cohort. Requires an operator or project supervisor; missing attribution and charges remain unknown.

    Args:
        body (GetBenchmarkReportRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetBenchmarkReportResponse | GetBenchmarkReportResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
