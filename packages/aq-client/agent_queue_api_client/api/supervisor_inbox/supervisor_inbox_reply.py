from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.supervisor_inbox_error_response import SupervisorInboxErrorResponse
from ...models.supervisor_inbox_reply_request import SupervisorInboxReplyRequest
from ...models.supervisor_inbox_reply_response import SupervisorInboxReplyResponse
from ...types import Response


def _get_kwargs(
    *,
    body: SupervisorInboxReplyRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/supervisor_inbox/reply",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> SupervisorInboxErrorResponse | SupervisorInboxReplyResponse | None:
    if response.status_code == 200:
        response_200 = SupervisorInboxReplyResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = SupervisorInboxErrorResponse.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[SupervisorInboxErrorResponse | SupervisorInboxReplyResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SupervisorInboxReplyRequest,
) -> Response[SupervisorInboxErrorResponse | SupervisorInboxReplyResponse]:
    """Explicit conversation reply from the live global supervisor or local operator.

     Explicit conversation reply from the live global supervisor or local operator.

    Args:
        body (SupervisorInboxReplyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[SupervisorInboxErrorResponse | SupervisorInboxReplyResponse]
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
    body: SupervisorInboxReplyRequest,
) -> SupervisorInboxErrorResponse | SupervisorInboxReplyResponse | None:
    """Explicit conversation reply from the live global supervisor or local operator.

     Explicit conversation reply from the live global supervisor or local operator.

    Args:
        body (SupervisorInboxReplyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        SupervisorInboxErrorResponse | SupervisorInboxReplyResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SupervisorInboxReplyRequest,
) -> Response[SupervisorInboxErrorResponse | SupervisorInboxReplyResponse]:
    """Explicit conversation reply from the live global supervisor or local operator.

     Explicit conversation reply from the live global supervisor or local operator.

    Args:
        body (SupervisorInboxReplyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[SupervisorInboxErrorResponse | SupervisorInboxReplyResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: SupervisorInboxReplyRequest,
) -> SupervisorInboxErrorResponse | SupervisorInboxReplyResponse | None:
    """Explicit conversation reply from the live global supervisor or local operator.

     Explicit conversation reply from the live global supervisor or local operator.

    Args:
        body (SupervisorInboxReplyRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        SupervisorInboxErrorResponse | SupervisorInboxReplyResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
