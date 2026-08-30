from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.session_input_request import SessionInputRequest
from ...models.session_input_response import SessionInputResponse
from ...models.session_input_response_422 import SessionInputResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: SessionInputRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/session-input",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> SessionInputResponse | SessionInputResponse422 | None:
    if response.status_code == 200:
        response_200 = SessionInputResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = SessionInputResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[SessionInputResponse | SessionInputResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SessionInputRequest,
) -> Response[SessionInputResponse | SessionInputResponse422]:
    """Type directly into a live terminal. Send exactly one literal text chunk or named key; text is not
    automatically submitted. Requires global admin.

     Type directly into a live terminal. Send exactly one literal text chunk or named key; text is not
    automatically submitted. Requires global admin.

    Args:
        body (SessionInputRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[SessionInputResponse | SessionInputResponse422]
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
    body: SessionInputRequest,
) -> SessionInputResponse | SessionInputResponse422 | None:
    """Type directly into a live terminal. Send exactly one literal text chunk or named key; text is not
    automatically submitted. Requires global admin.

     Type directly into a live terminal. Send exactly one literal text chunk or named key; text is not
    automatically submitted. Requires global admin.

    Args:
        body (SessionInputRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        SessionInputResponse | SessionInputResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SessionInputRequest,
) -> Response[SessionInputResponse | SessionInputResponse422]:
    """Type directly into a live terminal. Send exactly one literal text chunk or named key; text is not
    automatically submitted. Requires global admin.

     Type directly into a live terminal. Send exactly one literal text chunk or named key; text is not
    automatically submitted. Requires global admin.

    Args:
        body (SessionInputRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[SessionInputResponse | SessionInputResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: SessionInputRequest,
) -> SessionInputResponse | SessionInputResponse422 | None:
    """Type directly into a live terminal. Send exactly one literal text chunk or named key; text is not
    automatically submitted. Requires global admin.

     Type directly into a live terminal. Send exactly one literal text chunk or named key; text is not
    automatically submitted. Requires global admin.

    Args:
        body (SessionInputRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        SessionInputResponse | SessionInputResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
