from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_cutover_switch_request import PlaybookCutoverSwitchRequest
from ...models.playbook_cutover_switch_response import PlaybookCutoverSwitchResponse
from ...models.playbook_cutover_switch_response_422 import PlaybookCutoverSwitchResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookCutoverSwitchRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/cutover-switch",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookCutoverSwitchResponse | PlaybookCutoverSwitchResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookCutoverSwitchResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookCutoverSwitchResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookCutoverSwitchResponse | PlaybookCutoverSwitchResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookCutoverSwitchRequest,
) -> Response[PlaybookCutoverSwitchResponse | PlaybookCutoverSwitchResponse422]:
    """Move the fleet between the V1 and V2 playbook runtimes. Operator-only, and the highest-privilege
    operation in the subsystem. Switching to v2 is refused until the drain completes; switching back to
    v1 is the rollback and is refused once the rollback window has been closed.

     Move the fleet between the V1 and V2 playbook runtimes. Operator-only, and the highest-privilege
    operation in the subsystem. Switching to v2 is refused until the drain completes; switching back to
    v1 is the rollback and is refused once the rollback window has been closed.

    Args:
        body (PlaybookCutoverSwitchRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookCutoverSwitchResponse | PlaybookCutoverSwitchResponse422]
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
    body: PlaybookCutoverSwitchRequest,
) -> PlaybookCutoverSwitchResponse | PlaybookCutoverSwitchResponse422 | None:
    """Move the fleet between the V1 and V2 playbook runtimes. Operator-only, and the highest-privilege
    operation in the subsystem. Switching to v2 is refused until the drain completes; switching back to
    v1 is the rollback and is refused once the rollback window has been closed.

     Move the fleet between the V1 and V2 playbook runtimes. Operator-only, and the highest-privilege
    operation in the subsystem. Switching to v2 is refused until the drain completes; switching back to
    v1 is the rollback and is refused once the rollback window has been closed.

    Args:
        body (PlaybookCutoverSwitchRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookCutoverSwitchResponse | PlaybookCutoverSwitchResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookCutoverSwitchRequest,
) -> Response[PlaybookCutoverSwitchResponse | PlaybookCutoverSwitchResponse422]:
    """Move the fleet between the V1 and V2 playbook runtimes. Operator-only, and the highest-privilege
    operation in the subsystem. Switching to v2 is refused until the drain completes; switching back to
    v1 is the rollback and is refused once the rollback window has been closed.

     Move the fleet between the V1 and V2 playbook runtimes. Operator-only, and the highest-privilege
    operation in the subsystem. Switching to v2 is refused until the drain completes; switching back to
    v1 is the rollback and is refused once the rollback window has been closed.

    Args:
        body (PlaybookCutoverSwitchRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookCutoverSwitchResponse | PlaybookCutoverSwitchResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookCutoverSwitchRequest,
) -> PlaybookCutoverSwitchResponse | PlaybookCutoverSwitchResponse422 | None:
    """Move the fleet between the V1 and V2 playbook runtimes. Operator-only, and the highest-privilege
    operation in the subsystem. Switching to v2 is refused until the drain completes; switching back to
    v1 is the rollback and is refused once the rollback window has been closed.

     Move the fleet between the V1 and V2 playbook runtimes. Operator-only, and the highest-privilege
    operation in the subsystem. Switching to v2 is refused until the drain completes; switching back to
    v1 is the rollback and is refused once the rollback window has been closed.

    Args:
        body (PlaybookCutoverSwitchRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookCutoverSwitchResponse | PlaybookCutoverSwitchResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
