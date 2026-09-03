from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_cutover_window_rehearsal_request import PlaybookCutoverWindowRehearsalRequest
from ...models.playbook_cutover_window_rehearsal_response import PlaybookCutoverWindowRehearsalResponse
from ...models.playbook_cutover_window_rehearsal_response_422 import PlaybookCutoverWindowRehearsalResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookCutoverWindowRehearsalRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/cutover-window-rehearsal",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookCutoverWindowRehearsalResponse | PlaybookCutoverWindowRehearsalResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookCutoverWindowRehearsalResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookCutoverWindowRehearsalResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookCutoverWindowRehearsalResponse | PlaybookCutoverWindowRehearsalResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookCutoverWindowRehearsalRequest,
) -> Response[PlaybookCutoverWindowRehearsalResponse | PlaybookCutoverWindowRehearsalResponse422]:
    """Dispatch one synthetic live event per enabled playbook so an idle fleet can satisfy the observation
    window's coverage condition. Operator-only. Records a window_coverage_rehearsal audit row naming
    every enabled playbook, the runs it started and any it could not; a window later closed on this
    traffic says so. Also where the manual dashboard time-to-interactive review (measure 13) is
    recorded.

     Dispatch one synthetic live event per enabled playbook so an idle fleet can satisfy the observation
    window's coverage condition. Operator-only. Records a window_coverage_rehearsal audit row naming
    every enabled playbook, the runs it started and any it could not; a window later closed on this
    traffic says so. Also where the manual dashboard time-to-interactive review (measure 13) is
    recorded.

    Args:
        body (PlaybookCutoverWindowRehearsalRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookCutoverWindowRehearsalResponse | PlaybookCutoverWindowRehearsalResponse422]
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
    body: PlaybookCutoverWindowRehearsalRequest,
) -> PlaybookCutoverWindowRehearsalResponse | PlaybookCutoverWindowRehearsalResponse422 | None:
    """Dispatch one synthetic live event per enabled playbook so an idle fleet can satisfy the observation
    window's coverage condition. Operator-only. Records a window_coverage_rehearsal audit row naming
    every enabled playbook, the runs it started and any it could not; a window later closed on this
    traffic says so. Also where the manual dashboard time-to-interactive review (measure 13) is
    recorded.

     Dispatch one synthetic live event per enabled playbook so an idle fleet can satisfy the observation
    window's coverage condition. Operator-only. Records a window_coverage_rehearsal audit row naming
    every enabled playbook, the runs it started and any it could not; a window later closed on this
    traffic says so. Also where the manual dashboard time-to-interactive review (measure 13) is
    recorded.

    Args:
        body (PlaybookCutoverWindowRehearsalRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookCutoverWindowRehearsalResponse | PlaybookCutoverWindowRehearsalResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookCutoverWindowRehearsalRequest,
) -> Response[PlaybookCutoverWindowRehearsalResponse | PlaybookCutoverWindowRehearsalResponse422]:
    """Dispatch one synthetic live event per enabled playbook so an idle fleet can satisfy the observation
    window's coverage condition. Operator-only. Records a window_coverage_rehearsal audit row naming
    every enabled playbook, the runs it started and any it could not; a window later closed on this
    traffic says so. Also where the manual dashboard time-to-interactive review (measure 13) is
    recorded.

     Dispatch one synthetic live event per enabled playbook so an idle fleet can satisfy the observation
    window's coverage condition. Operator-only. Records a window_coverage_rehearsal audit row naming
    every enabled playbook, the runs it started and any it could not; a window later closed on this
    traffic says so. Also where the manual dashboard time-to-interactive review (measure 13) is
    recorded.

    Args:
        body (PlaybookCutoverWindowRehearsalRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookCutoverWindowRehearsalResponse | PlaybookCutoverWindowRehearsalResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookCutoverWindowRehearsalRequest,
) -> PlaybookCutoverWindowRehearsalResponse | PlaybookCutoverWindowRehearsalResponse422 | None:
    """Dispatch one synthetic live event per enabled playbook so an idle fleet can satisfy the observation
    window's coverage condition. Operator-only. Records a window_coverage_rehearsal audit row naming
    every enabled playbook, the runs it started and any it could not; a window later closed on this
    traffic says so. Also where the manual dashboard time-to-interactive review (measure 13) is
    recorded.

     Dispatch one synthetic live event per enabled playbook so an idle fleet can satisfy the observation
    window's coverage condition. Operator-only. Records a window_coverage_rehearsal audit row naming
    every enabled playbook, the runs it started and any it could not; a window later closed on this
    traffic says so. Also where the manual dashboard time-to-interactive review (measure 13) is
    recorded.

    Args:
        body (PlaybookCutoverWindowRehearsalRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookCutoverWindowRehearsalResponse | PlaybookCutoverWindowRehearsalResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
