from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.escalation_error_response import EscalationErrorResponse
from ...models.escalation_sweep_request import EscalationSweepRequest
from ...models.escalation_sweep_response import EscalationSweepResponse
from ...types import Response


def _get_kwargs(
    *,
    body: EscalationSweepRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/escalation/sweep",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> EscalationErrorResponse | EscalationSweepResponse | None:
    if response.status_code == 200:
        response_200 = EscalationSweepResponse.from_dict(response.json())

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
) -> Response[EscalationErrorResponse | EscalationSweepResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: EscalationSweepRequest,
) -> Response[EscalationErrorResponse | EscalationSweepResponse]:
    """Plan the §5.6 back-fill sweep over the escalation pile and, with apply, run it. Without apply it is
    a dry run that writes nothing: it returns the plan per escalation, what it would close, and what it
    would list for supervisor triage. Gated by discord.escalations.stateful; idempotent, so a second run
    is a no-op.

     Plan the §5.6 back-fill sweep over the escalation pile and, with apply, run it. Without apply it is
    a dry run that writes nothing: it returns the plan per escalation, what it would close, and what it
    would list for supervisor triage. Gated by discord.escalations.stateful; idempotent, so a second run
    is a no-op.

    Args:
        body (EscalationSweepRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EscalationErrorResponse | EscalationSweepResponse]
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
    body: EscalationSweepRequest,
) -> EscalationErrorResponse | EscalationSweepResponse | None:
    """Plan the §5.6 back-fill sweep over the escalation pile and, with apply, run it. Without apply it is
    a dry run that writes nothing: it returns the plan per escalation, what it would close, and what it
    would list for supervisor triage. Gated by discord.escalations.stateful; idempotent, so a second run
    is a no-op.

     Plan the §5.6 back-fill sweep over the escalation pile and, with apply, run it. Without apply it is
    a dry run that writes nothing: it returns the plan per escalation, what it would close, and what it
    would list for supervisor triage. Gated by discord.escalations.stateful; idempotent, so a second run
    is a no-op.

    Args:
        body (EscalationSweepRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EscalationErrorResponse | EscalationSweepResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: EscalationSweepRequest,
) -> Response[EscalationErrorResponse | EscalationSweepResponse]:
    """Plan the §5.6 back-fill sweep over the escalation pile and, with apply, run it. Without apply it is
    a dry run that writes nothing: it returns the plan per escalation, what it would close, and what it
    would list for supervisor triage. Gated by discord.escalations.stateful; idempotent, so a second run
    is a no-op.

     Plan the §5.6 back-fill sweep over the escalation pile and, with apply, run it. Without apply it is
    a dry run that writes nothing: it returns the plan per escalation, what it would close, and what it
    would list for supervisor triage. Gated by discord.escalations.stateful; idempotent, so a second run
    is a no-op.

    Args:
        body (EscalationSweepRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[EscalationErrorResponse | EscalationSweepResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: EscalationSweepRequest,
) -> EscalationErrorResponse | EscalationSweepResponse | None:
    """Plan the §5.6 back-fill sweep over the escalation pile and, with apply, run it. Without apply it is
    a dry run that writes nothing: it returns the plan per escalation, what it would close, and what it
    would list for supervisor triage. Gated by discord.escalations.stateful; idempotent, so a second run
    is a no-op.

     Plan the §5.6 back-fill sweep over the escalation pile and, with apply, run it. Without apply it is
    a dry run that writes nothing: it returns the plan per escalation, what it would close, and what it
    would list for supervisor triage. Gated by discord.escalations.stateful; idempotent, so a second run
    is a no-op.

    Args:
        body (EscalationSweepRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        EscalationErrorResponse | EscalationSweepResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
