from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.run_playbook_request import RunPlaybookRequest
from ...models.run_playbook_response import RunPlaybookResponse
from ...models.run_playbook_response_422 import RunPlaybookResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: RunPlaybookRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/run",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> RunPlaybookResponse | RunPlaybookResponse422 | None:
    if response.status_code == 200:
        response_200 = RunPlaybookResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = RunPlaybookResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[RunPlaybookResponse | RunPlaybookResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: RunPlaybookRequest,
) -> Response[RunPlaybookResponse | RunPlaybookResponse422]:
    """Manually trigger a playbook run. Executes the full compiled playbook graph from entry to terminal
    with real LLM calls, database persistence, and event emission. Use this to start a playbook on
    demand rather than waiting for its configured trigger (timer, event, etc.).

     Manually trigger a playbook run. Executes the full compiled playbook graph from entry to terminal
    with real LLM calls, database persistence, and event emission. Use this to start a playbook on
    demand rather than waiting for its configured trigger (timer, event, etc.).

    Args:
        body (RunPlaybookRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[RunPlaybookResponse | RunPlaybookResponse422]
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
    body: RunPlaybookRequest,
) -> RunPlaybookResponse | RunPlaybookResponse422 | None:
    """Manually trigger a playbook run. Executes the full compiled playbook graph from entry to terminal
    with real LLM calls, database persistence, and event emission. Use this to start a playbook on
    demand rather than waiting for its configured trigger (timer, event, etc.).

     Manually trigger a playbook run. Executes the full compiled playbook graph from entry to terminal
    with real LLM calls, database persistence, and event emission. Use this to start a playbook on
    demand rather than waiting for its configured trigger (timer, event, etc.).

    Args:
        body (RunPlaybookRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        RunPlaybookResponse | RunPlaybookResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: RunPlaybookRequest,
) -> Response[RunPlaybookResponse | RunPlaybookResponse422]:
    """Manually trigger a playbook run. Executes the full compiled playbook graph from entry to terminal
    with real LLM calls, database persistence, and event emission. Use this to start a playbook on
    demand rather than waiting for its configured trigger (timer, event, etc.).

     Manually trigger a playbook run. Executes the full compiled playbook graph from entry to terminal
    with real LLM calls, database persistence, and event emission. Use this to start a playbook on
    demand rather than waiting for its configured trigger (timer, event, etc.).

    Args:
        body (RunPlaybookRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[RunPlaybookResponse | RunPlaybookResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: RunPlaybookRequest,
) -> RunPlaybookResponse | RunPlaybookResponse422 | None:
    """Manually trigger a playbook run. Executes the full compiled playbook graph from entry to terminal
    with real LLM calls, database persistence, and event emission. Use this to start a playbook on
    demand rather than waiting for its configured trigger (timer, event, etc.).

     Manually trigger a playbook run. Executes the full compiled playbook graph from entry to terminal
    with real LLM calls, database persistence, and event emission. Use this to start a playbook on
    demand rather than waiting for its configured trigger (timer, event, etc.).

    Args:
        body (RunPlaybookRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        RunPlaybookResponse | RunPlaybookResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
