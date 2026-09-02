from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_activation_health_request import PlaybookActivationHealthRequest
from ...models.playbook_activation_health_response import PlaybookActivationHealthResponse
from ...models.playbook_activation_health_response_422 import PlaybookActivationHealthResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookActivationHealthRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/activation-health",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookActivationHealthResponse | PlaybookActivationHealthResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookActivationHealthResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookActivationHealthResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookActivationHealthResponse | PlaybookActivationHealthResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookActivationHealthRequest,
) -> Response[PlaybookActivationHealthResponse | PlaybookActivationHealthResponse422]:
    """List playbook V2 activations with their computed health (ready, question_required, invalid,
    disabled, stale_contract, unavailable), the active artifact hash, machine-readable reasons, and
    pending/running counts. Enabled and health are independent: a disabled activation still reports the
    health it would have.

     List playbook V2 activations with their computed health (ready, question_required, invalid,
    disabled, stale_contract, unavailable), the active artifact hash, machine-readable reasons, and
    pending/running counts. Enabled and health are independent: a disabled activation still reports the
    health it would have.

    Args:
        body (PlaybookActivationHealthRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookActivationHealthResponse | PlaybookActivationHealthResponse422]
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
    body: PlaybookActivationHealthRequest,
) -> PlaybookActivationHealthResponse | PlaybookActivationHealthResponse422 | None:
    """List playbook V2 activations with their computed health (ready, question_required, invalid,
    disabled, stale_contract, unavailable), the active artifact hash, machine-readable reasons, and
    pending/running counts. Enabled and health are independent: a disabled activation still reports the
    health it would have.

     List playbook V2 activations with their computed health (ready, question_required, invalid,
    disabled, stale_contract, unavailable), the active artifact hash, machine-readable reasons, and
    pending/running counts. Enabled and health are independent: a disabled activation still reports the
    health it would have.

    Args:
        body (PlaybookActivationHealthRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookActivationHealthResponse | PlaybookActivationHealthResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookActivationHealthRequest,
) -> Response[PlaybookActivationHealthResponse | PlaybookActivationHealthResponse422]:
    """List playbook V2 activations with their computed health (ready, question_required, invalid,
    disabled, stale_contract, unavailable), the active artifact hash, machine-readable reasons, and
    pending/running counts. Enabled and health are independent: a disabled activation still reports the
    health it would have.

     List playbook V2 activations with their computed health (ready, question_required, invalid,
    disabled, stale_contract, unavailable), the active artifact hash, machine-readable reasons, and
    pending/running counts. Enabled and health are independent: a disabled activation still reports the
    health it would have.

    Args:
        body (PlaybookActivationHealthRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookActivationHealthResponse | PlaybookActivationHealthResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookActivationHealthRequest,
) -> PlaybookActivationHealthResponse | PlaybookActivationHealthResponse422 | None:
    """List playbook V2 activations with their computed health (ready, question_required, invalid,
    disabled, stale_contract, unavailable), the active artifact hash, machine-readable reasons, and
    pending/running counts. Enabled and health are independent: a disabled activation still reports the
    health it would have.

     List playbook V2 activations with their computed health (ready, question_required, invalid,
    disabled, stale_contract, unavailable), the active artifact hash, machine-readable reasons, and
    pending/running counts. Enabled and health are independent: a disabled activation still reports the
    health it would have.

    Args:
        body (PlaybookActivationHealthRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookActivationHealthResponse | PlaybookActivationHealthResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
