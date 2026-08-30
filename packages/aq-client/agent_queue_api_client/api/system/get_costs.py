from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.get_costs_request import GetCostsRequest
from ...models.get_costs_response_422 import GetCostsResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: GetCostsRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/get-costs",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | GetCostsResponse422 | None:
    if response.status_code == 200:
        response_200 = response.json()
        return response_200

    if response.status_code == 422:
        response_422 = GetCostsResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Any | GetCostsResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetCostsRequest,
) -> Response[Any | GetCostsResponse422]:
    """Roll the token ledger up into USD using the 'pricing:' table from config.yaml.  Rows are grouped by
    project (default), profile or day, and split per model.  Honesty rule: a row is priced only when it
    carries both a model matching a pricing entry and an input/output token split — everything else is
    reported under 'unpriced_tokens' with a null cost rather than priced at a guessed rate.

     Roll the token ledger up into USD using the 'pricing:' table from config.yaml.  Rows are grouped by
    project (default), profile or day, and split per model.  Honesty rule: a row is priced only when it
    carries both a model matching a pricing entry and an input/output token split — everything else is
    reported under 'unpriced_tokens' with a null cost rather than priced at a guessed rate.

    Args:
        body (GetCostsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | GetCostsResponse422]
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
    body: GetCostsRequest,
) -> Any | GetCostsResponse422 | None:
    """Roll the token ledger up into USD using the 'pricing:' table from config.yaml.  Rows are grouped by
    project (default), profile or day, and split per model.  Honesty rule: a row is priced only when it
    carries both a model matching a pricing entry and an input/output token split — everything else is
    reported under 'unpriced_tokens' with a null cost rather than priced at a guessed rate.

     Roll the token ledger up into USD using the 'pricing:' table from config.yaml.  Rows are grouped by
    project (default), profile or day, and split per model.  Honesty rule: a row is priced only when it
    carries both a model matching a pricing entry and an input/output token split — everything else is
    reported under 'unpriced_tokens' with a null cost rather than priced at a guessed rate.

    Args:
        body (GetCostsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | GetCostsResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: GetCostsRequest,
) -> Response[Any | GetCostsResponse422]:
    """Roll the token ledger up into USD using the 'pricing:' table from config.yaml.  Rows are grouped by
    project (default), profile or day, and split per model.  Honesty rule: a row is priced only when it
    carries both a model matching a pricing entry and an input/output token split — everything else is
    reported under 'unpriced_tokens' with a null cost rather than priced at a guessed rate.

     Roll the token ledger up into USD using the 'pricing:' table from config.yaml.  Rows are grouped by
    project (default), profile or day, and split per model.  Honesty rule: a row is priced only when it
    carries both a model matching a pricing entry and an input/output token split — everything else is
    reported under 'unpriced_tokens' with a null cost rather than priced at a guessed rate.

    Args:
        body (GetCostsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | GetCostsResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: GetCostsRequest,
) -> Any | GetCostsResponse422 | None:
    """Roll the token ledger up into USD using the 'pricing:' table from config.yaml.  Rows are grouped by
    project (default), profile or day, and split per model.  Honesty rule: a row is priced only when it
    carries both a model matching a pricing entry and an input/output token split — everything else is
    reported under 'unpriced_tokens' with a null cost rather than priced at a guessed rate.

     Roll the token ledger up into USD using the 'pricing:' table from config.yaml.  Rows are grouped by
    project (default), profile or day, and split per model.  Honesty rule: a row is priced only when it
    carries both a model matching a pricing entry and an input/output token split — everything else is
    reported under 'unpriced_tokens' with a null cost rather than priced at a guessed rate.

    Args:
        body (GetCostsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | GetCostsResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
