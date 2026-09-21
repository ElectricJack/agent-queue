from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.phase_list_request import PhaseListRequest
from ...models.phase_list_response import PhaseListResponse
from ...models.phase_list_response_422 import PhaseListResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PhaseListRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/phase-list",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PhaseListResponse | PhaseListResponse422 | None:
    if response.status_code == 200:
        response_200 = PhaseListResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PhaseListResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PhaseListResponse | PhaseListResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PhaseListRequest,
) -> Response[PhaseListResponse | PhaseListResponse422]:
    """List a project's phases in order, with each phase's status, blocked state and child counts.

     List a project's phases in order, with each phase's status, blocked state and child counts.

    Args:
        body (PhaseListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PhaseListResponse | PhaseListResponse422]
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
    body: PhaseListRequest,
) -> PhaseListResponse | PhaseListResponse422 | None:
    """List a project's phases in order, with each phase's status, blocked state and child counts.

     List a project's phases in order, with each phase's status, blocked state and child counts.

    Args:
        body (PhaseListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PhaseListResponse | PhaseListResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PhaseListRequest,
) -> Response[PhaseListResponse | PhaseListResponse422]:
    """List a project's phases in order, with each phase's status, blocked state and child counts.

     List a project's phases in order, with each phase's status, blocked state and child counts.

    Args:
        body (PhaseListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PhaseListResponse | PhaseListResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PhaseListRequest,
) -> PhaseListResponse | PhaseListResponse422 | None:
    """List a project's phases in order, with each phase's status, blocked state and child counts.

     List a project's phases in order, with each phase's status, blocked state and child counts.

    Args:
        body (PhaseListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PhaseListResponse | PhaseListResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
