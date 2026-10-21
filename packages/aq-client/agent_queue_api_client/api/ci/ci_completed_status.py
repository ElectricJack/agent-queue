from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.ci_completed_status_request import CiCompletedStatusRequest
from ...models.ci_completed_status_response import CiCompletedStatusResponse
from ...models.ci_completed_status_response_422 import CiCompletedStatusResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: CiCompletedStatusRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/ci/completed-status",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> CiCompletedStatusResponse | CiCompletedStatusResponse422 | None:
    if response.status_code == 200:
        response_200 = CiCompletedStatusResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = CiCompletedStatusResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[CiCompletedStatusResponse | CiCompletedStatusResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CiCompletedStatusRequest,
) -> Response[CiCompletedStatusResponse | CiCompletedStatusResponse422]:
    """Audit COMPLETED tasks' recorded source commits against the project's required CI. List failing
    checks separately from pending or unavailable observations, with exact SHAs and check links. Read-
    only; operator or supervisor only.

     Audit COMPLETED tasks' recorded source commits against the project's required CI. List failing
    checks separately from pending or unavailable observations, with exact SHAs and check links. Read-
    only; operator or supervisor only.

    Args:
        body (CiCompletedStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CiCompletedStatusResponse | CiCompletedStatusResponse422]
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
    body: CiCompletedStatusRequest,
) -> CiCompletedStatusResponse | CiCompletedStatusResponse422 | None:
    """Audit COMPLETED tasks' recorded source commits against the project's required CI. List failing
    checks separately from pending or unavailable observations, with exact SHAs and check links. Read-
    only; operator or supervisor only.

     Audit COMPLETED tasks' recorded source commits against the project's required CI. List failing
    checks separately from pending or unavailable observations, with exact SHAs and check links. Read-
    only; operator or supervisor only.

    Args:
        body (CiCompletedStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CiCompletedStatusResponse | CiCompletedStatusResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CiCompletedStatusRequest,
) -> Response[CiCompletedStatusResponse | CiCompletedStatusResponse422]:
    """Audit COMPLETED tasks' recorded source commits against the project's required CI. List failing
    checks separately from pending or unavailable observations, with exact SHAs and check links. Read-
    only; operator or supervisor only.

     Audit COMPLETED tasks' recorded source commits against the project's required CI. List failing
    checks separately from pending or unavailable observations, with exact SHAs and check links. Read-
    only; operator or supervisor only.

    Args:
        body (CiCompletedStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CiCompletedStatusResponse | CiCompletedStatusResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: CiCompletedStatusRequest,
) -> CiCompletedStatusResponse | CiCompletedStatusResponse422 | None:
    """Audit COMPLETED tasks' recorded source commits against the project's required CI. List failing
    checks separately from pending or unavailable observations, with exact SHAs and check links. Read-
    only; operator or supervisor only.

     Audit COMPLETED tasks' recorded source commits against the project's required CI. List failing
    checks separately from pending or unavailable observations, with exact SHAs and check links. Read-
    only; operator or supervisor only.

    Args:
        body (CiCompletedStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CiCompletedStatusResponse | CiCompletedStatusResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
