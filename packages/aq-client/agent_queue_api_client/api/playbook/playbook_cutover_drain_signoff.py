from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_cutover_drain_signoff_request import PlaybookCutoverDrainSignoffRequest
from ...models.playbook_cutover_drain_signoff_response import PlaybookCutoverDrainSignoffResponse
from ...models.playbook_cutover_drain_signoff_response_422 import PlaybookCutoverDrainSignoffResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookCutoverDrainSignoffRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/cutover-drain-signoff",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookCutoverDrainSignoffResponse | PlaybookCutoverDrainSignoffResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookCutoverDrainSignoffResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookCutoverDrainSignoffResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookCutoverDrainSignoffResponse | PlaybookCutoverDrainSignoffResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookCutoverDrainSignoffRequest,
) -> Response[PlaybookCutoverDrainSignoffResponse | PlaybookCutoverDrainSignoffResponse422]:
    """Gate G1: a named human signs off the V1 drain. Operator-only. The command re-verifies every
    readiness check itself and refuses while any one blocks, naming it; a second sign-off for the same
    attempt is refused. Appends a drain_completed audit row carrying the attested name, the readiness
    table it verified and the V1 latency baseline.

     Gate G1: a named human signs off the V1 drain. Operator-only. The command re-verifies every
    readiness check itself and refuses while any one blocks, naming it; a second sign-off for the same
    attempt is refused. Appends a drain_completed audit row carrying the attested name, the readiness
    table it verified and the V1 latency baseline.

    Args:
        body (PlaybookCutoverDrainSignoffRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookCutoverDrainSignoffResponse | PlaybookCutoverDrainSignoffResponse422]
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
    body: PlaybookCutoverDrainSignoffRequest,
) -> PlaybookCutoverDrainSignoffResponse | PlaybookCutoverDrainSignoffResponse422 | None:
    """Gate G1: a named human signs off the V1 drain. Operator-only. The command re-verifies every
    readiness check itself and refuses while any one blocks, naming it; a second sign-off for the same
    attempt is refused. Appends a drain_completed audit row carrying the attested name, the readiness
    table it verified and the V1 latency baseline.

     Gate G1: a named human signs off the V1 drain. Operator-only. The command re-verifies every
    readiness check itself and refuses while any one blocks, naming it; a second sign-off for the same
    attempt is refused. Appends a drain_completed audit row carrying the attested name, the readiness
    table it verified and the V1 latency baseline.

    Args:
        body (PlaybookCutoverDrainSignoffRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookCutoverDrainSignoffResponse | PlaybookCutoverDrainSignoffResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookCutoverDrainSignoffRequest,
) -> Response[PlaybookCutoverDrainSignoffResponse | PlaybookCutoverDrainSignoffResponse422]:
    """Gate G1: a named human signs off the V1 drain. Operator-only. The command re-verifies every
    readiness check itself and refuses while any one blocks, naming it; a second sign-off for the same
    attempt is refused. Appends a drain_completed audit row carrying the attested name, the readiness
    table it verified and the V1 latency baseline.

     Gate G1: a named human signs off the V1 drain. Operator-only. The command re-verifies every
    readiness check itself and refuses while any one blocks, naming it; a second sign-off for the same
    attempt is refused. Appends a drain_completed audit row carrying the attested name, the readiness
    table it verified and the V1 latency baseline.

    Args:
        body (PlaybookCutoverDrainSignoffRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookCutoverDrainSignoffResponse | PlaybookCutoverDrainSignoffResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookCutoverDrainSignoffRequest,
) -> PlaybookCutoverDrainSignoffResponse | PlaybookCutoverDrainSignoffResponse422 | None:
    """Gate G1: a named human signs off the V1 drain. Operator-only. The command re-verifies every
    readiness check itself and refuses while any one blocks, naming it; a second sign-off for the same
    attempt is refused. Appends a drain_completed audit row carrying the attested name, the readiness
    table it verified and the V1 latency baseline.

     Gate G1: a named human signs off the V1 drain. Operator-only. The command re-verifies every
    readiness check itself and refuses while any one blocks, naming it; a second sign-off for the same
    attempt is refused. Appends a drain_completed audit row carrying the attested name, the readiness
    table it verified and the V1 latency baseline.

    Args:
        body (PlaybookCutoverDrainSignoffRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookCutoverDrainSignoffResponse | PlaybookCutoverDrainSignoffResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
