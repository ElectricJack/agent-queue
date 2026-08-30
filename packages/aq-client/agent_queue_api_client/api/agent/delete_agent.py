from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.delete_agent_request import DeleteAgentRequest
from ...models.delete_agent_response import DeleteAgentResponse
from ...models.delete_agent_response_422 import DeleteAgentResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: DeleteAgentRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/agent/delete",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> DeleteAgentResponse | DeleteAgentResponse422 | None:
    if response.status_code == 200:
        response_200 = DeleteAgentResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = DeleteAgentResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[DeleteAgentResponse | DeleteAgentResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DeleteAgentRequest,
) -> Response[DeleteAgentResponse | DeleteAgentResponse422]:
    """Delete an idle shared worker from the flock while preserving task and session history. Active
    workers and the global supervisor cannot be deleted. Requires global admin.

     Delete an idle shared worker from the flock while preserving task and session history. Active
    workers and the global supervisor cannot be deleted. Requires global admin.

    Args:
        body (DeleteAgentRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DeleteAgentResponse | DeleteAgentResponse422]
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
    body: DeleteAgentRequest,
) -> DeleteAgentResponse | DeleteAgentResponse422 | None:
    """Delete an idle shared worker from the flock while preserving task and session history. Active
    workers and the global supervisor cannot be deleted. Requires global admin.

     Delete an idle shared worker from the flock while preserving task and session history. Active
    workers and the global supervisor cannot be deleted. Requires global admin.

    Args:
        body (DeleteAgentRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DeleteAgentResponse | DeleteAgentResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DeleteAgentRequest,
) -> Response[DeleteAgentResponse | DeleteAgentResponse422]:
    """Delete an idle shared worker from the flock while preserving task and session history. Active
    workers and the global supervisor cannot be deleted. Requires global admin.

     Delete an idle shared worker from the flock while preserving task and session history. Active
    workers and the global supervisor cannot be deleted. Requires global admin.

    Args:
        body (DeleteAgentRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DeleteAgentResponse | DeleteAgentResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: DeleteAgentRequest,
) -> DeleteAgentResponse | DeleteAgentResponse422 | None:
    """Delete an idle shared worker from the flock while preserving task and session history. Active
    workers and the global supervisor cannot be deleted. Requires global admin.

     Delete an idle shared worker from the flock while preserving task and session history. Active
    workers and the global supervisor cannot be deleted. Requires global admin.

    Args:
        body (DeleteAgentRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DeleteAgentResponse | DeleteAgentResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
