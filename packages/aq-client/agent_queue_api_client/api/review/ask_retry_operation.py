from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.ask_retry_operation_request import AskRetryOperationRequest
from ...models.ask_retry_operation_response_422 import AskRetryOperationResponse422
from ...models.review_decide_response import ReviewDecideResponse
from ...types import Response


def _get_kwargs(
    *,
    body: AskRetryOperationRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/review/ask-retry-operation",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> AskRetryOperationResponse422 | ReviewDecideResponse | None:
    if response.status_code == 200:
        response_200 = ReviewDecideResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = AskRetryOperationResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[AskRetryOperationResponse422 | ReviewDecideResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: AskRetryOperationRequest,
) -> Response[AskRetryOperationResponse422 | ReviewDecideResponse]:
    """Operator reconciliation of an already accepted decision; never creates approval.

     Operator reconciliation of an already accepted decision; never creates approval.

    Args:
        body (AskRetryOperationRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[AskRetryOperationResponse422 | ReviewDecideResponse]
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
    body: AskRetryOperationRequest,
) -> AskRetryOperationResponse422 | ReviewDecideResponse | None:
    """Operator reconciliation of an already accepted decision; never creates approval.

     Operator reconciliation of an already accepted decision; never creates approval.

    Args:
        body (AskRetryOperationRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        AskRetryOperationResponse422 | ReviewDecideResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: AskRetryOperationRequest,
) -> Response[AskRetryOperationResponse422 | ReviewDecideResponse]:
    """Operator reconciliation of an already accepted decision; never creates approval.

     Operator reconciliation of an already accepted decision; never creates approval.

    Args:
        body (AskRetryOperationRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[AskRetryOperationResponse422 | ReviewDecideResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: AskRetryOperationRequest,
) -> AskRetryOperationResponse422 | ReviewDecideResponse | None:
    """Operator reconciliation of an already accepted decision; never creates approval.

     Operator reconciliation of an already accepted decision; never creates approval.

    Args:
        body (AskRetryOperationRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        AskRetryOperationResponse422 | ReviewDecideResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
