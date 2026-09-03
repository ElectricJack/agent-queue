from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_v1_drain_status_request import PlaybookV1DrainStatusRequest
from ...models.playbook_v1_drain_status_response_422 import PlaybookV1DrainStatusResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookV1DrainStatusRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/v1-drain-status",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | PlaybookV1DrainStatusResponse422 | None:
    if response.status_code == 200:
        response_200 = response.json()
        return response_200

    if response.status_code == 422:
        response_422 = PlaybookV1DrainStatusResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Any | PlaybookV1DrainStatusResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookV1DrainStatusRequest,
) -> Response[Any | PlaybookV1DrainStatusResponse422]:
    """List every non-terminal V1 playbook run, each classified live (a coroutine owns it) or orphaned (the
    row outlived the process that started it, so only an operator write can clear it), with the options
    available for each. Read-only. Answers while playbooks are paused, because a paused fleet with
    running rows is exactly the one that needs draining. 'drained' requires admission closed AND zero
    active runs -- a zero count alone is a snapshot, not a gate.

     List every non-terminal V1 playbook run, each classified live (a coroutine owns it) or orphaned (the
    row outlived the process that started it, so only an operator write can clear it), with the options
    available for each. Read-only. Answers while playbooks are paused, because a paused fleet with
    running rows is exactly the one that needs draining. 'drained' requires admission closed AND zero
    active runs -- a zero count alone is a snapshot, not a gate.

    Args:
        body (PlaybookV1DrainStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | PlaybookV1DrainStatusResponse422]
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
    body: PlaybookV1DrainStatusRequest,
) -> Any | PlaybookV1DrainStatusResponse422 | None:
    """List every non-terminal V1 playbook run, each classified live (a coroutine owns it) or orphaned (the
    row outlived the process that started it, so only an operator write can clear it), with the options
    available for each. Read-only. Answers while playbooks are paused, because a paused fleet with
    running rows is exactly the one that needs draining. 'drained' requires admission closed AND zero
    active runs -- a zero count alone is a snapshot, not a gate.

     List every non-terminal V1 playbook run, each classified live (a coroutine owns it) or orphaned (the
    row outlived the process that started it, so only an operator write can clear it), with the options
    available for each. Read-only. Answers while playbooks are paused, because a paused fleet with
    running rows is exactly the one that needs draining. 'drained' requires admission closed AND zero
    active runs -- a zero count alone is a snapshot, not a gate.

    Args:
        body (PlaybookV1DrainStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | PlaybookV1DrainStatusResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookV1DrainStatusRequest,
) -> Response[Any | PlaybookV1DrainStatusResponse422]:
    """List every non-terminal V1 playbook run, each classified live (a coroutine owns it) or orphaned (the
    row outlived the process that started it, so only an operator write can clear it), with the options
    available for each. Read-only. Answers while playbooks are paused, because a paused fleet with
    running rows is exactly the one that needs draining. 'drained' requires admission closed AND zero
    active runs -- a zero count alone is a snapshot, not a gate.

     List every non-terminal V1 playbook run, each classified live (a coroutine owns it) or orphaned (the
    row outlived the process that started it, so only an operator write can clear it), with the options
    available for each. Read-only. Answers while playbooks are paused, because a paused fleet with
    running rows is exactly the one that needs draining. 'drained' requires admission closed AND zero
    active runs -- a zero count alone is a snapshot, not a gate.

    Args:
        body (PlaybookV1DrainStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | PlaybookV1DrainStatusResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookV1DrainStatusRequest,
) -> Any | PlaybookV1DrainStatusResponse422 | None:
    """List every non-terminal V1 playbook run, each classified live (a coroutine owns it) or orphaned (the
    row outlived the process that started it, so only an operator write can clear it), with the options
    available for each. Read-only. Answers while playbooks are paused, because a paused fleet with
    running rows is exactly the one that needs draining. 'drained' requires admission closed AND zero
    active runs -- a zero count alone is a snapshot, not a gate.

     List every non-terminal V1 playbook run, each classified live (a coroutine owns it) or orphaned (the
    row outlived the process that started it, so only an operator write can clear it), with the options
    available for each. Read-only. Answers while playbooks are paused, because a paused fleet with
    running rows is exactly the one that needs draining. 'drained' requires admission closed AND zero
    active runs -- a zero count alone is a snapshot, not a gate.

    Args:
        body (PlaybookV1DrainStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | PlaybookV1DrainStatusResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
