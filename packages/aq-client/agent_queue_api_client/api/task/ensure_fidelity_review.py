from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.ensure_fidelity_review_request import EnsureFidelityReviewRequest
from ...models.ensure_fidelity_review_response_422 import EnsureFidelityReviewResponse422
from ...models.ensure_task_response import EnsureTaskResponse
from ...types import Response


def _get_kwargs(
    *,
    body: EnsureFidelityReviewRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/ensure-fidelity-review",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> EnsureFidelityReviewResponse422 | EnsureTaskResponse | None:
    if response.status_code == 200:
        response_200 = EnsureTaskResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = EnsureFidelityReviewResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[EnsureFidelityReviewResponse422 | EnsureTaskResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: EnsureFidelityReviewRequest,
) -> Response[EnsureFidelityReviewResponse422 | EnsureTaskResponse]:
    """Ensure one fidelity-reviewer role task per held batch; service/operator only.

     Ensure one fidelity-reviewer role task per held batch; service/operator only.

    Args:
        body (EnsureFidelityReviewRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EnsureFidelityReviewResponse422 | EnsureTaskResponse]
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
    body: EnsureFidelityReviewRequest,
) -> EnsureFidelityReviewResponse422 | EnsureTaskResponse | None:
    """Ensure one fidelity-reviewer role task per held batch; service/operator only.

     Ensure one fidelity-reviewer role task per held batch; service/operator only.

    Args:
        body (EnsureFidelityReviewRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EnsureFidelityReviewResponse422 | EnsureTaskResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: EnsureFidelityReviewRequest,
) -> Response[EnsureFidelityReviewResponse422 | EnsureTaskResponse]:
    """Ensure one fidelity-reviewer role task per held batch; service/operator only.

     Ensure one fidelity-reviewer role task per held batch; service/operator only.

    Args:
        body (EnsureFidelityReviewRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EnsureFidelityReviewResponse422 | EnsureTaskResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: EnsureFidelityReviewRequest,
) -> EnsureFidelityReviewResponse422 | EnsureTaskResponse | None:
    """Ensure one fidelity-reviewer role task per held batch; service/operator only.

     Ensure one fidelity-reviewer role task per held batch; service/operator only.

    Args:
        body (EnsureFidelityReviewRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EnsureFidelityReviewResponse422 | EnsureTaskResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
