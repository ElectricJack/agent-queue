from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.ask_human_request import AskHumanRequest
from ...models.ask_human_response_422 import AskHumanResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: AskHumanRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/ask-human",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | AskHumanResponse422 | None:
    if response.status_code == 200:
        response_200 = response.json()
        return response_200

    if response.status_code == 422:
        response_422 = AskHumanResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Any | AskHumanResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: AskHumanRequest,
) -> Response[Any | AskHumanResponse422]:
    """Ask a human a question via a human gate, blocking on their response. Backs `aq task ask`.
    (Implementation pending.)

     Ask a human a question via a human gate, blocking on their response. Backs `aq task ask`.
    (Implementation pending.)

    Args:
        body (AskHumanRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | AskHumanResponse422]
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
    body: AskHumanRequest,
) -> Any | AskHumanResponse422 | None:
    """Ask a human a question via a human gate, blocking on their response. Backs `aq task ask`.
    (Implementation pending.)

     Ask a human a question via a human gate, blocking on their response. Backs `aq task ask`.
    (Implementation pending.)

    Args:
        body (AskHumanRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | AskHumanResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: AskHumanRequest,
) -> Response[Any | AskHumanResponse422]:
    """Ask a human a question via a human gate, blocking on their response. Backs `aq task ask`.
    (Implementation pending.)

     Ask a human a question via a human gate, blocking on their response. Backs `aq task ask`.
    (Implementation pending.)

    Args:
        body (AskHumanRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | AskHumanResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: AskHumanRequest,
) -> Any | AskHumanResponse422 | None:
    """Ask a human a question via a human gate, blocking on their response. Backs `aq task ask`.
    (Implementation pending.)

     Ask a human a question via a human gate, blocking on their response. Backs `aq task ask`.
    (Implementation pending.)

    Args:
        body (AskHumanRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | AskHumanResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
