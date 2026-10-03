from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.record_capabilities_request import RecordCapabilitiesRequest
from ...models.record_capabilities_response import RecordCapabilitiesResponse
from ...models.record_capabilities_response_422 import RecordCapabilitiesResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: RecordCapabilitiesRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/record/capabilities",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> RecordCapabilitiesResponse | RecordCapabilitiesResponse422 | None:
    if response.status_code == 200:
        response_200 = RecordCapabilitiesResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = RecordCapabilitiesResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[RecordCapabilitiesResponse | RecordCapabilitiesResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: RecordCapabilitiesRequest,
) -> Response[RecordCapabilitiesResponse | RecordCapabilitiesResponse422]:
    """Describe what this caller may do with records in the project.

     Describe what this caller may do with records in the project.

    Args:
        body (RecordCapabilitiesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[RecordCapabilitiesResponse | RecordCapabilitiesResponse422]
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
    body: RecordCapabilitiesRequest,
) -> RecordCapabilitiesResponse | RecordCapabilitiesResponse422 | None:
    """Describe what this caller may do with records in the project.

     Describe what this caller may do with records in the project.

    Args:
        body (RecordCapabilitiesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        RecordCapabilitiesResponse | RecordCapabilitiesResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: RecordCapabilitiesRequest,
) -> Response[RecordCapabilitiesResponse | RecordCapabilitiesResponse422]:
    """Describe what this caller may do with records in the project.

     Describe what this caller may do with records in the project.

    Args:
        body (RecordCapabilitiesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[RecordCapabilitiesResponse | RecordCapabilitiesResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: RecordCapabilitiesRequest,
) -> RecordCapabilitiesResponse | RecordCapabilitiesResponse422 | None:
    """Describe what this caller may do with records in the project.

     Describe what this caller may do with records in the project.

    Args:
        body (RecordCapabilitiesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        RecordCapabilitiesResponse | RecordCapabilitiesResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
