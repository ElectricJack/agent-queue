from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.provide_input_request import ProvideInputRequest
from ...models.provide_input_response import ProvideInputResponse
from ...models.provide_input_response_422 import ProvideInputResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ProvideInputRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/provide-input",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ProvideInputResponse | ProvideInputResponse422 | None:
    if response.status_code == 200:
        response_200 = ProvideInputResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ProvideInputResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ProvideInputResponse | ProvideInputResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProvideInputRequest,
) -> Response[ProvideInputResponse | ProvideInputResponse422]:
    """Provide a human reply to an agent question (WAITING_INPUT → READY). The agent's question is answered
    by appending the human's response to the task description so the agent sees it on re-execution.

     Provide a human reply to an agent question (WAITING_INPUT → READY). The agent's question is answered
    by appending the human's response to the task description so the agent sees it on re-execution.

    Args:
        body (ProvideInputRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProvideInputResponse | ProvideInputResponse422]
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
    body: ProvideInputRequest,
) -> ProvideInputResponse | ProvideInputResponse422 | None:
    """Provide a human reply to an agent question (WAITING_INPUT → READY). The agent's question is answered
    by appending the human's response to the task description so the agent sees it on re-execution.

     Provide a human reply to an agent question (WAITING_INPUT → READY). The agent's question is answered
    by appending the human's response to the task description so the agent sees it on re-execution.

    Args:
        body (ProvideInputRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProvideInputResponse | ProvideInputResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProvideInputRequest,
) -> Response[ProvideInputResponse | ProvideInputResponse422]:
    """Provide a human reply to an agent question (WAITING_INPUT → READY). The agent's question is answered
    by appending the human's response to the task description so the agent sees it on re-execution.

     Provide a human reply to an agent question (WAITING_INPUT → READY). The agent's question is answered
    by appending the human's response to the task description so the agent sees it on re-execution.

    Args:
        body (ProvideInputRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProvideInputResponse | ProvideInputResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ProvideInputRequest,
) -> ProvideInputResponse | ProvideInputResponse422 | None:
    """Provide a human reply to an agent question (WAITING_INPUT → READY). The agent's question is answered
    by appending the human's response to the task description so the agent sees it on re-execution.

     Provide a human reply to an agent question (WAITING_INPUT → READY). The agent's question is answered
    by appending the human's response to the task description so the agent sees it on re-execution.

    Args:
        body (ProvideInputRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProvideInputResponse | ProvideInputResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
