from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_activate_request import PlaybookActivateRequest
from ...models.playbook_activate_response_422 import PlaybookActivateResponse422
from ...models.set_playbook_activation_response import SetPlaybookActivationResponse
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookActivateRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/activate",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookActivateResponse422 | SetPlaybookActivationResponse | None:
    if response.status_code == 200:
        response_200 = SetPlaybookActivationResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookActivateResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookActivateResponse422 | SetPlaybookActivationResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookActivateRequest,
) -> Response[PlaybookActivateResponse422 | SetPlaybookActivationResponse]:
    """Activate one reviewed Playbook V2 artifact hash. Activation is an explicit operation against a
    reviewed artifact -- compilation never activates. Refused when playbooks.v2_activation_writes is
    off, when the artifact's health is invalid, or when the diff against the currently active artifact
    carries an executable change and acknowledge_diff was not supplied. Every refusal returns
    blocked=true with machine-readable blockers.

     Activate one reviewed Playbook V2 artifact hash. Activation is an explicit operation against a
    reviewed artifact -- compilation never activates. Refused when playbooks.v2_activation_writes is
    off, when the artifact's health is invalid, or when the diff against the currently active artifact
    carries an executable change and acknowledge_diff was not supplied. Every refusal returns
    blocked=true with machine-readable blockers.

    Args:
        body (PlaybookActivateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookActivateResponse422 | SetPlaybookActivationResponse]
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
    body: PlaybookActivateRequest,
) -> PlaybookActivateResponse422 | SetPlaybookActivationResponse | None:
    """Activate one reviewed Playbook V2 artifact hash. Activation is an explicit operation against a
    reviewed artifact -- compilation never activates. Refused when playbooks.v2_activation_writes is
    off, when the artifact's health is invalid, or when the diff against the currently active artifact
    carries an executable change and acknowledge_diff was not supplied. Every refusal returns
    blocked=true with machine-readable blockers.

     Activate one reviewed Playbook V2 artifact hash. Activation is an explicit operation against a
    reviewed artifact -- compilation never activates. Refused when playbooks.v2_activation_writes is
    off, when the artifact's health is invalid, or when the diff against the currently active artifact
    carries an executable change and acknowledge_diff was not supplied. Every refusal returns
    blocked=true with machine-readable blockers.

    Args:
        body (PlaybookActivateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookActivateResponse422 | SetPlaybookActivationResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookActivateRequest,
) -> Response[PlaybookActivateResponse422 | SetPlaybookActivationResponse]:
    """Activate one reviewed Playbook V2 artifact hash. Activation is an explicit operation against a
    reviewed artifact -- compilation never activates. Refused when playbooks.v2_activation_writes is
    off, when the artifact's health is invalid, or when the diff against the currently active artifact
    carries an executable change and acknowledge_diff was not supplied. Every refusal returns
    blocked=true with machine-readable blockers.

     Activate one reviewed Playbook V2 artifact hash. Activation is an explicit operation against a
    reviewed artifact -- compilation never activates. Refused when playbooks.v2_activation_writes is
    off, when the artifact's health is invalid, or when the diff against the currently active artifact
    carries an executable change and acknowledge_diff was not supplied. Every refusal returns
    blocked=true with machine-readable blockers.

    Args:
        body (PlaybookActivateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookActivateResponse422 | SetPlaybookActivationResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookActivateRequest,
) -> PlaybookActivateResponse422 | SetPlaybookActivationResponse | None:
    """Activate one reviewed Playbook V2 artifact hash. Activation is an explicit operation against a
    reviewed artifact -- compilation never activates. Refused when playbooks.v2_activation_writes is
    off, when the artifact's health is invalid, or when the diff against the currently active artifact
    carries an executable change and acknowledge_diff was not supplied. Every refusal returns
    blocked=true with machine-readable blockers.

     Activate one reviewed Playbook V2 artifact hash. Activation is an explicit operation against a
    reviewed artifact -- compilation never activates. Refused when playbooks.v2_activation_writes is
    off, when the artifact's health is invalid, or when the diff against the currently active artifact
    carries an executable change and acknowledge_diff was not supplied. Every refusal returns
    blocked=true with machine-readable blockers.

    Args:
        body (PlaybookActivateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookActivateResponse422 | SetPlaybookActivationResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
