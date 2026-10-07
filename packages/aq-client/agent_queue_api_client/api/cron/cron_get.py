from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.cron_get_request import CronGetRequest
from ...models.cron_get_response_422 import CronGetResponse422
from ...models.cron_response import CronResponse
from ...types import Response


def _get_kwargs(
    *,
    body: CronGetRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/cron/get",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> CronGetResponse422 | CronResponse | None:
    if response.status_code == 200:
        response_200 = CronResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = CronGetResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[CronGetResponse422 | CronResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CronGetRequest,
) -> Response[CronGetResponse422 | CronResponse]:
    """Read prompt and next-fire/delivery diagnostics; optionally consume its wake.

     Read prompt and next-fire/delivery diagnostics; optionally consume its wake.

    Args:
        body (CronGetRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CronGetResponse422 | CronResponse]
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
    body: CronGetRequest,
) -> CronGetResponse422 | CronResponse | None:
    """Read prompt and next-fire/delivery diagnostics; optionally consume its wake.

     Read prompt and next-fire/delivery diagnostics; optionally consume its wake.

    Args:
        body (CronGetRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CronGetResponse422 | CronResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CronGetRequest,
) -> Response[CronGetResponse422 | CronResponse]:
    """Read prompt and next-fire/delivery diagnostics; optionally consume its wake.

     Read prompt and next-fire/delivery diagnostics; optionally consume its wake.

    Args:
        body (CronGetRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CronGetResponse422 | CronResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: CronGetRequest,
) -> CronGetResponse422 | CronResponse | None:
    """Read prompt and next-fire/delivery diagnostics; optionally consume its wake.

     Read prompt and next-fire/delivery diagnostics; optionally consume its wake.

    Args:
        body (CronGetRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CronGetResponse422 | CronResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
