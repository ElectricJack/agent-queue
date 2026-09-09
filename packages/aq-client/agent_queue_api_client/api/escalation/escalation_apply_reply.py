from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.escalation_apply_reply_request import EscalationApplyReplyRequest
from ...models.escalation_apply_reply_response import EscalationApplyReplyResponse
from ...models.escalation_error_response import EscalationErrorResponse
from ...types import Response


def _get_kwargs(
    *,
    body: EscalationApplyReplyRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/escalation/apply-reply",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> EscalationApplyReplyResponse | EscalationErrorResponse | None:
    if response.status_code == 200:
        response_200 = EscalationApplyReplyResponse.from_dict(response.json())

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
) -> Response[EscalationApplyReplyResponse | EscalationErrorResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: EscalationApplyReplyRequest,
) -> Response[EscalationApplyReplyResponse | EscalationErrorResponse]:
    """Apply one bound verified human reply through the owning supervisor's exact question, human-gate, or
    task-recovery service.

     Apply one bound verified human reply through the owning supervisor's exact question, human-gate, or
    task-recovery service.

    Args:
        body (EscalationApplyReplyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EscalationApplyReplyResponse | EscalationErrorResponse]
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
    body: EscalationApplyReplyRequest,
) -> EscalationApplyReplyResponse | EscalationErrorResponse | None:
    """Apply one bound verified human reply through the owning supervisor's exact question, human-gate, or
    task-recovery service.

     Apply one bound verified human reply through the owning supervisor's exact question, human-gate, or
    task-recovery service.

    Args:
        body (EscalationApplyReplyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EscalationApplyReplyResponse | EscalationErrorResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: EscalationApplyReplyRequest,
) -> Response[EscalationApplyReplyResponse | EscalationErrorResponse]:
    """Apply one bound verified human reply through the owning supervisor's exact question, human-gate, or
    task-recovery service.

     Apply one bound verified human reply through the owning supervisor's exact question, human-gate, or
    task-recovery service.

    Args:
        body (EscalationApplyReplyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EscalationApplyReplyResponse | EscalationErrorResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: EscalationApplyReplyRequest,
) -> EscalationApplyReplyResponse | EscalationErrorResponse | None:
    """Apply one bound verified human reply through the owning supervisor's exact question, human-gate, or
    task-recovery service.

     Apply one bound verified human reply through the owning supervisor's exact question, human-gate, or
    task-recovery service.

    Args:
        body (EscalationApplyReplyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EscalationApplyReplyResponse | EscalationErrorResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
