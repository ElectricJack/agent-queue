from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_release_check_request import PlaybookReleaseCheckRequest
from ...models.playbook_release_check_response import PlaybookReleaseCheckResponse
from ...models.playbook_release_check_response_422 import PlaybookReleaseCheckResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookReleaseCheckRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/release-check",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookReleaseCheckResponse | PlaybookReleaseCheckResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookReleaseCheckResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookReleaseCheckResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookReleaseCheckResponse | PlaybookReleaseCheckResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookReleaseCheckRequest,
) -> Response[PlaybookReleaseCheckResponse | PlaybookReleaseCheckResponse422]:
    """Check that every reviewed V2 artifact still matches the command contracts it was compiled against.
    Compares the checked-in reviewed fixtures and every enabled activation against the live registry,
    and names each command whose execution fingerprint moved. Offline and read-only: no network, no LLM,
    no compile. A presentation-only label change does not trip it.

     Check that every reviewed V2 artifact still matches the command contracts it was compiled against.
    Compares the checked-in reviewed fixtures and every enabled activation against the live registry,
    and names each command whose execution fingerprint moved. Offline and read-only: no network, no LLM,
    no compile. A presentation-only label change does not trip it.

    Args:
        body (PlaybookReleaseCheckRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookReleaseCheckResponse | PlaybookReleaseCheckResponse422]
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
    body: PlaybookReleaseCheckRequest,
) -> PlaybookReleaseCheckResponse | PlaybookReleaseCheckResponse422 | None:
    """Check that every reviewed V2 artifact still matches the command contracts it was compiled against.
    Compares the checked-in reviewed fixtures and every enabled activation against the live registry,
    and names each command whose execution fingerprint moved. Offline and read-only: no network, no LLM,
    no compile. A presentation-only label change does not trip it.

     Check that every reviewed V2 artifact still matches the command contracts it was compiled against.
    Compares the checked-in reviewed fixtures and every enabled activation against the live registry,
    and names each command whose execution fingerprint moved. Offline and read-only: no network, no LLM,
    no compile. A presentation-only label change does not trip it.

    Args:
        body (PlaybookReleaseCheckRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookReleaseCheckResponse | PlaybookReleaseCheckResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookReleaseCheckRequest,
) -> Response[PlaybookReleaseCheckResponse | PlaybookReleaseCheckResponse422]:
    """Check that every reviewed V2 artifact still matches the command contracts it was compiled against.
    Compares the checked-in reviewed fixtures and every enabled activation against the live registry,
    and names each command whose execution fingerprint moved. Offline and read-only: no network, no LLM,
    no compile. A presentation-only label change does not trip it.

     Check that every reviewed V2 artifact still matches the command contracts it was compiled against.
    Compares the checked-in reviewed fixtures and every enabled activation against the live registry,
    and names each command whose execution fingerprint moved. Offline and read-only: no network, no LLM,
    no compile. A presentation-only label change does not trip it.

    Args:
        body (PlaybookReleaseCheckRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookReleaseCheckResponse | PlaybookReleaseCheckResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookReleaseCheckRequest,
) -> PlaybookReleaseCheckResponse | PlaybookReleaseCheckResponse422 | None:
    """Check that every reviewed V2 artifact still matches the command contracts it was compiled against.
    Compares the checked-in reviewed fixtures and every enabled activation against the live registry,
    and names each command whose execution fingerprint moved. Offline and read-only: no network, no LLM,
    no compile. A presentation-only label change does not trip it.

     Check that every reviewed V2 artifact still matches the command contracts it was compiled against.
    Compares the checked-in reviewed fixtures and every enabled activation against the live registry,
    and names each command whose execution fingerprint moved. Offline and read-only: no network, no LLM,
    no compile. A presentation-only label change does not trip it.

    Args:
        body (PlaybookReleaseCheckRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookReleaseCheckResponse | PlaybookReleaseCheckResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
