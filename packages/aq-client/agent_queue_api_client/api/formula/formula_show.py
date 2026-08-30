from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.formula_show_request import FormulaShowRequest
from ...models.formula_show_response import FormulaShowResponse
from ...models.formula_show_response_422 import FormulaShowResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: FormulaShowRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/formula/show",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> FormulaShowResponse | FormulaShowResponse422 | None:
    if response.status_code == 200:
        response_200 = FormulaShowResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = FormulaShowResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[FormulaShowResponse | FormulaShowResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: FormulaShowRequest,
) -> Response[FormulaShowResponse | FormulaShowResponse422]:
    """Resolve a formula's extends chain, substitute its vars and validate the result — read-only, never
    writes. With as_cooked=<container_id>, instead render back the formula_snapshot a previous
    formula_cook actually wrote for that container, ignoring the current vault file.

     Resolve a formula's extends chain, substitute its vars and validate the result — read-only, never
    writes. With as_cooked=<container_id>, instead render back the formula_snapshot a previous
    formula_cook actually wrote for that container, ignoring the current vault file.

    Args:
        body (FormulaShowRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[FormulaShowResponse | FormulaShowResponse422]
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
    body: FormulaShowRequest,
) -> FormulaShowResponse | FormulaShowResponse422 | None:
    """Resolve a formula's extends chain, substitute its vars and validate the result — read-only, never
    writes. With as_cooked=<container_id>, instead render back the formula_snapshot a previous
    formula_cook actually wrote for that container, ignoring the current vault file.

     Resolve a formula's extends chain, substitute its vars and validate the result — read-only, never
    writes. With as_cooked=<container_id>, instead render back the formula_snapshot a previous
    formula_cook actually wrote for that container, ignoring the current vault file.

    Args:
        body (FormulaShowRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        FormulaShowResponse | FormulaShowResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: FormulaShowRequest,
) -> Response[FormulaShowResponse | FormulaShowResponse422]:
    """Resolve a formula's extends chain, substitute its vars and validate the result — read-only, never
    writes. With as_cooked=<container_id>, instead render back the formula_snapshot a previous
    formula_cook actually wrote for that container, ignoring the current vault file.

     Resolve a formula's extends chain, substitute its vars and validate the result — read-only, never
    writes. With as_cooked=<container_id>, instead render back the formula_snapshot a previous
    formula_cook actually wrote for that container, ignoring the current vault file.

    Args:
        body (FormulaShowRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[FormulaShowResponse | FormulaShowResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: FormulaShowRequest,
) -> FormulaShowResponse | FormulaShowResponse422 | None:
    """Resolve a formula's extends chain, substitute its vars and validate the result — read-only, never
    writes. With as_cooked=<container_id>, instead render back the formula_snapshot a previous
    formula_cook actually wrote for that container, ignoring the current vault file.

     Resolve a formula's extends chain, substitute its vars and validate the result — read-only, never
    writes. With as_cooked=<container_id>, instead render back the formula_snapshot a previous
    formula_cook actually wrote for that container, ignoring the current vault file.

    Args:
        body (FormulaShowRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        FormulaShowResponse | FormulaShowResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
