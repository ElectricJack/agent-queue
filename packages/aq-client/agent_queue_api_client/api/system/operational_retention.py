from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.operational_retention_request import OperationalRetentionRequest
from ...models.operational_retention_response import OperationalRetentionResponse
from ...models.operational_retention_response_422 import OperationalRetentionResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: OperationalRetentionRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/operational-retention",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> OperationalRetentionResponse | OperationalRetentionResponse422 | None:
    if response.status_code == 200:
        response_200 = OperationalRetentionResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = OperationalRetentionResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[OperationalRetentionResponse | OperationalRetentionResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: OperationalRetentionRequest,
) -> Response[OperationalRetentionResponse | OperationalRetentionResponse422]:
    """Archive and collect one bounded batch of old operational records.

     Archive and collect one bounded batch of old operational records.

    Args:
        body (OperationalRetentionRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[OperationalRetentionResponse | OperationalRetentionResponse422]
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
    body: OperationalRetentionRequest,
) -> OperationalRetentionResponse | OperationalRetentionResponse422 | None:
    """Archive and collect one bounded batch of old operational records.

     Archive and collect one bounded batch of old operational records.

    Args:
        body (OperationalRetentionRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        OperationalRetentionResponse | OperationalRetentionResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: OperationalRetentionRequest,
) -> Response[OperationalRetentionResponse | OperationalRetentionResponse422]:
    """Archive and collect one bounded batch of old operational records.

     Archive and collect one bounded batch of old operational records.

    Args:
        body (OperationalRetentionRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[OperationalRetentionResponse | OperationalRetentionResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: OperationalRetentionRequest,
) -> OperationalRetentionResponse | OperationalRetentionResponse422 | None:
    """Archive and collect one bounded batch of old operational records.

     Archive and collect one bounded batch of old operational records.

    Args:
        body (OperationalRetentionRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        OperationalRetentionResponse | OperationalRetentionResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
