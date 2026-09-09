from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.escalation_error_response import EscalationErrorResponse
from ...models.escalation_get_request import EscalationGetRequest
from ...models.escalation_get_response import EscalationGetResponse
from ...types import Response


def _get_kwargs(
    *,
    body: EscalationGetRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/escalation/get",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> EscalationErrorResponse | EscalationGetResponse | None:
    if response.status_code == 200:
        response_200 = EscalationGetResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = EscalationErrorResponse.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[EscalationErrorResponse | EscalationGetResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: EscalationGetRequest,
) -> Response[EscalationErrorResponse | EscalationGetResponse]:
    """Get one visible escalation with immutable messages, deliveries, and actions.

     Get one visible escalation with immutable messages, deliveries, and actions.

    Args:
        body (EscalationGetRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EscalationErrorResponse | EscalationGetResponse]
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
    body: EscalationGetRequest,
) -> EscalationErrorResponse | EscalationGetResponse | None:
    """Get one visible escalation with immutable messages, deliveries, and actions.

     Get one visible escalation with immutable messages, deliveries, and actions.

    Args:
        body (EscalationGetRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EscalationErrorResponse | EscalationGetResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: EscalationGetRequest,
) -> Response[EscalationErrorResponse | EscalationGetResponse]:
    """Get one visible escalation with immutable messages, deliveries, and actions.

     Get one visible escalation with immutable messages, deliveries, and actions.

    Args:
        body (EscalationGetRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EscalationErrorResponse | EscalationGetResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: EscalationGetRequest,
) -> EscalationErrorResponse | EscalationGetResponse | None:
    """Get one visible escalation with immutable messages, deliveries, and actions.

     Get one visible escalation with immutable messages, deliveries, and actions.

    Args:
        body (EscalationGetRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EscalationErrorResponse | EscalationGetResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
