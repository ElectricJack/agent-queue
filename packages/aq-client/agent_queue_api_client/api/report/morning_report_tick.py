from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.morning_report_tick_request import MorningReportTickRequest
from ...models.morning_report_tick_response import MorningReportTickResponse
from ...models.morning_report_tick_response_422 import MorningReportTickResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: MorningReportTickRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/report/morning-tick",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> MorningReportTickResponse | MorningReportTickResponse422 | None:
    if response.status_code == 200:
        response_200 = MorningReportTickResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = MorningReportTickResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[MorningReportTickResponse | MorningReportTickResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: MorningReportTickRequest,
) -> Response[MorningReportTickResponse | MorningReportTickResponse422]:
    """Reserve/recover a daily morning report (service/system playbook only).

     Reserve/recover a daily morning report (service/system playbook only).

    Args:
        body (MorningReportTickRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[MorningReportTickResponse | MorningReportTickResponse422]
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
    body: MorningReportTickRequest,
) -> MorningReportTickResponse | MorningReportTickResponse422 | None:
    """Reserve/recover a daily morning report (service/system playbook only).

     Reserve/recover a daily morning report (service/system playbook only).

    Args:
        body (MorningReportTickRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        MorningReportTickResponse | MorningReportTickResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: MorningReportTickRequest,
) -> Response[MorningReportTickResponse | MorningReportTickResponse422]:
    """Reserve/recover a daily morning report (service/system playbook only).

     Reserve/recover a daily morning report (service/system playbook only).

    Args:
        body (MorningReportTickRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[MorningReportTickResponse | MorningReportTickResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: MorningReportTickRequest,
) -> MorningReportTickResponse | MorningReportTickResponse422 | None:
    """Reserve/recover a daily morning report (service/system playbook only).

     Reserve/recover a daily morning report (service/system playbook only).

    Args:
        body (MorningReportTickRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        MorningReportTickResponse | MorningReportTickResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
