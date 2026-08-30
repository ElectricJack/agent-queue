from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.doctor_request import DoctorRequest
from ...models.doctor_response_422 import DoctorResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: DoctorRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/doctor",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | DoctorResponse422 | None:
    if response.status_code == 200:
        response_200 = response.json()
        return response_200

    if response.status_code == 422:
        response_422 = DoctorResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Any | DoctorResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DoctorRequest,
) -> Response[Any | DoctorResponse422]:
    """Run the health-check catalog for this install and return one result per check: id, severity
    (ok/info/warn/error), detail, whether it is fixable, and structured extras.  Checks run concurrently
    with per-check timeouts; a check that crashes or times out reports 'error' rather than hanging the
    command.  Pass fix=true to apply the fix of each failing fixable check and re-run it (fixes are
    idempotent and only touch derived state — WAL, expired log dirs — never tasks, vault files or
    branches).  The returned exit_code is 2 when any check errored, 1 when any warned, otherwise 0.

     Run the health-check catalog for this install and return one result per check: id, severity
    (ok/info/warn/error), detail, whether it is fixable, and structured extras.  Checks run concurrently
    with per-check timeouts; a check that crashes or times out reports 'error' rather than hanging the
    command.  Pass fix=true to apply the fix of each failing fixable check and re-run it (fixes are
    idempotent and only touch derived state — WAL, expired log dirs — never tasks, vault files or
    branches).  The returned exit_code is 2 when any check errored, 1 when any warned, otherwise 0.

    Args:
        body (DoctorRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | DoctorResponse422]
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
    body: DoctorRequest,
) -> Any | DoctorResponse422 | None:
    """Run the health-check catalog for this install and return one result per check: id, severity
    (ok/info/warn/error), detail, whether it is fixable, and structured extras.  Checks run concurrently
    with per-check timeouts; a check that crashes or times out reports 'error' rather than hanging the
    command.  Pass fix=true to apply the fix of each failing fixable check and re-run it (fixes are
    idempotent and only touch derived state — WAL, expired log dirs — never tasks, vault files or
    branches).  The returned exit_code is 2 when any check errored, 1 when any warned, otherwise 0.

     Run the health-check catalog for this install and return one result per check: id, severity
    (ok/info/warn/error), detail, whether it is fixable, and structured extras.  Checks run concurrently
    with per-check timeouts; a check that crashes or times out reports 'error' rather than hanging the
    command.  Pass fix=true to apply the fix of each failing fixable check and re-run it (fixes are
    idempotent and only touch derived state — WAL, expired log dirs — never tasks, vault files or
    branches).  The returned exit_code is 2 when any check errored, 1 when any warned, otherwise 0.

    Args:
        body (DoctorRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | DoctorResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DoctorRequest,
) -> Response[Any | DoctorResponse422]:
    """Run the health-check catalog for this install and return one result per check: id, severity
    (ok/info/warn/error), detail, whether it is fixable, and structured extras.  Checks run concurrently
    with per-check timeouts; a check that crashes or times out reports 'error' rather than hanging the
    command.  Pass fix=true to apply the fix of each failing fixable check and re-run it (fixes are
    idempotent and only touch derived state — WAL, expired log dirs — never tasks, vault files or
    branches).  The returned exit_code is 2 when any check errored, 1 when any warned, otherwise 0.

     Run the health-check catalog for this install and return one result per check: id, severity
    (ok/info/warn/error), detail, whether it is fixable, and structured extras.  Checks run concurrently
    with per-check timeouts; a check that crashes or times out reports 'error' rather than hanging the
    command.  Pass fix=true to apply the fix of each failing fixable check and re-run it (fixes are
    idempotent and only touch derived state — WAL, expired log dirs — never tasks, vault files or
    branches).  The returned exit_code is 2 when any check errored, 1 when any warned, otherwise 0.

    Args:
        body (DoctorRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | DoctorResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: DoctorRequest,
) -> Any | DoctorResponse422 | None:
    """Run the health-check catalog for this install and return one result per check: id, severity
    (ok/info/warn/error), detail, whether it is fixable, and structured extras.  Checks run concurrently
    with per-check timeouts; a check that crashes or times out reports 'error' rather than hanging the
    command.  Pass fix=true to apply the fix of each failing fixable check and re-run it (fixes are
    idempotent and only touch derived state — WAL, expired log dirs — never tasks, vault files or
    branches).  The returned exit_code is 2 when any check errored, 1 when any warned, otherwise 0.

     Run the health-check catalog for this install and return one result per check: id, severity
    (ok/info/warn/error), detail, whether it is fixable, and structured extras.  Checks run concurrently
    with per-check timeouts; a check that crashes or times out reports 'error' rather than hanging the
    command.  Pass fix=true to apply the fix of each failing fixable check and re-run it (fixes are
    idempotent and only touch derived state — WAL, expired log dirs — never tasks, vault files or
    branches).  The returned exit_code is 2 when any check errored, 1 when any warned, otherwise 0.

    Args:
        body (DoctorRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | DoctorResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
