from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.ask_error_response import AskErrorResponse
from ...models.ask_list_request import AskListRequest
from ...models.ask_list_response import AskListResponse
from ...types import Response


def _get_kwargs(
    *,
    body: AskListRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/review/ask-list",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> AskErrorResponse | AskListResponse | None:
    if response.status_code == 200:
        response_200 = AskListResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = AskErrorResponse.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[AskErrorResponse | AskListResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: AskListRequest,
) -> Response[AskErrorResponse | AskListResponse]:
    """Scoped human request communication and decision evidence.

     Scoped human request communication and decision evidence.

    Args:
        body (AskListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[AskErrorResponse | AskListResponse]
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
    body: AskListRequest,
) -> AskErrorResponse | AskListResponse | None:
    """Scoped human request communication and decision evidence.

     Scoped human request communication and decision evidence.

    Args:
        body (AskListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        AskErrorResponse | AskListResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: AskListRequest,
) -> Response[AskErrorResponse | AskListResponse]:
    """Scoped human request communication and decision evidence.

     Scoped human request communication and decision evidence.

    Args:
        body (AskListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[AskErrorResponse | AskListResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: AskListRequest,
) -> AskErrorResponse | AskListResponse | None:
    """Scoped human request communication and decision evidence.

     Scoped human request communication and decision evidence.

    Args:
        body (AskListRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        AskErrorResponse | AskListResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
