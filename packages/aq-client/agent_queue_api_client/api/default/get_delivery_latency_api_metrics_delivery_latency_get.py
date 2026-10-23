from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.delivery_latency_response import DeliveryLatencyResponse
from ...models.http_validation_error import HTTPValidationError
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    window: str | Unset = "24h",
    project_id: None | str | Unset = UNSET,
    until: float | None | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["window"] = window

    json_project_id: None | str | Unset
    if isinstance(project_id, Unset):
        json_project_id = UNSET
    else:
        json_project_id = project_id
    params["project_id"] = json_project_id

    json_until: float | None | Unset
    if isinstance(until, Unset):
        json_until = UNSET
    else:
        json_until = until
    params["until"] = json_until

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/metrics/delivery-latency",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> DeliveryLatencyResponse | HTTPValidationError | None:
    if response.status_code == 200:
        response_200 = DeliveryLatencyResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[DeliveryLatencyResponse | HTTPValidationError]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    window: str | Unset = "24h",
    project_id: None | str | Unset = UNSET,
    until: float | None | Unset = UNSET,
) -> Response[DeliveryLatencyResponse | HTTPValidationError]:
    """Get Delivery Latency

    Args:
        window (str | Unset):  Default: '24h'.
        project_id (None | str | Unset):
        until (float | None | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DeliveryLatencyResponse | HTTPValidationError]
    """

    kwargs = _get_kwargs(
        window=window,
        project_id=project_id,
        until=until,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    window: str | Unset = "24h",
    project_id: None | str | Unset = UNSET,
    until: float | None | Unset = UNSET,
) -> DeliveryLatencyResponse | HTTPValidationError | None:
    """Get Delivery Latency

    Args:
        window (str | Unset):  Default: '24h'.
        project_id (None | str | Unset):
        until (float | None | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DeliveryLatencyResponse | HTTPValidationError
    """

    return sync_detailed(
        client=client,
        window=window,
        project_id=project_id,
        until=until,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    window: str | Unset = "24h",
    project_id: None | str | Unset = UNSET,
    until: float | None | Unset = UNSET,
) -> Response[DeliveryLatencyResponse | HTTPValidationError]:
    """Get Delivery Latency

    Args:
        window (str | Unset):  Default: '24h'.
        project_id (None | str | Unset):
        until (float | None | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DeliveryLatencyResponse | HTTPValidationError]
    """

    kwargs = _get_kwargs(
        window=window,
        project_id=project_id,
        until=until,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    window: str | Unset = "24h",
    project_id: None | str | Unset = UNSET,
    until: float | None | Unset = UNSET,
) -> DeliveryLatencyResponse | HTTPValidationError | None:
    """Get Delivery Latency

    Args:
        window (str | Unset):  Default: '24h'.
        project_id (None | str | Unset):
        until (float | None | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DeliveryLatencyResponse | HTTPValidationError
    """

    return (
        await asyncio_detailed(
            client=client,
            window=window,
            project_id=project_id,
            until=until,
        )
    ).parsed
