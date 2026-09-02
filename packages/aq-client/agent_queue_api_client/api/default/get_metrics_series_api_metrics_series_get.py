from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.http_validation_error import HTTPValidationError
from ...models.metrics_series_response import MetricsSeriesResponse
from ...types import UNSET, Response, Unset


def _get_kwargs(
    *,
    from_: float | None | Unset = UNSET,
    to: float | None | Unset = UNSET,
    step: str | Unset = "auto",
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_from_: float | None | Unset
    if isinstance(from_, Unset):
        json_from_ = UNSET
    else:
        json_from_ = from_
    params["from"] = json_from_

    json_to: float | None | Unset
    if isinstance(to, Unset):
        json_to = UNSET
    else:
        json_to = to
    params["to"] = json_to

    params["step"] = step

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/metrics/series",
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> HTTPValidationError | MetricsSeriesResponse | None:
    if response.status_code == 200:
        response_200 = MetricsSeriesResponse.from_dict(response.json())

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
) -> Response[HTTPValidationError | MetricsSeriesResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    from_: float | None | Unset = UNSET,
    to: float | None | Unset = UNSET,
    step: str | Unset = "auto",
) -> Response[HTTPValidationError | MetricsSeriesResponse]:
    """Get Metrics Series

    Args:
        from_ (float | None | Unset):
        to (float | None | Unset):
        step (str | Unset):  Default: 'auto'.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | MetricsSeriesResponse]
    """

    kwargs = _get_kwargs(
        from_=from_,
        to=to,
        step=step,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    *,
    client: AuthenticatedClient | Client,
    from_: float | None | Unset = UNSET,
    to: float | None | Unset = UNSET,
    step: str | Unset = "auto",
) -> HTTPValidationError | MetricsSeriesResponse | None:
    """Get Metrics Series

    Args:
        from_ (float | None | Unset):
        to (float | None | Unset):
        step (str | Unset):  Default: 'auto'.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | MetricsSeriesResponse
    """

    return sync_detailed(
        client=client,
        from_=from_,
        to=to,
        step=step,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    from_: float | None | Unset = UNSET,
    to: float | None | Unset = UNSET,
    step: str | Unset = "auto",
) -> Response[HTTPValidationError | MetricsSeriesResponse]:
    """Get Metrics Series

    Args:
        from_ (float | None | Unset):
        to (float | None | Unset):
        step (str | Unset):  Default: 'auto'.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | MetricsSeriesResponse]
    """

    kwargs = _get_kwargs(
        from_=from_,
        to=to,
        step=step,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    from_: float | None | Unset = UNSET,
    to: float | None | Unset = UNSET,
    step: str | Unset = "auto",
) -> HTTPValidationError | MetricsSeriesResponse | None:
    """Get Metrics Series

    Args:
        from_ (float | None | Unset):
        to (float | None | Unset):
        step (str | Unset):  Default: 'auto'.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | MetricsSeriesResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            from_=from_,
            to=to,
            step=step,
        )
    ).parsed
