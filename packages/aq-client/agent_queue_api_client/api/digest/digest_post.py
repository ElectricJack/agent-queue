from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.digest_post_request import DigestPostRequest
from ...models.digest_post_response import DigestPostResponse
from ...models.escalation_error_response import EscalationErrorResponse
from ...types import Response


def _get_kwargs(
    *,
    body: DigestPostRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/digest/post",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> DigestPostResponse | EscalationErrorResponse | None:
    if response.status_code == 200:
        response_200 = DigestPostResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = EscalationErrorResponse.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[DigestPostResponse | EscalationErrorResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DigestPostRequest,
) -> Response[DigestPostResponse | EscalationErrorResponse]:
    """Post the supervisor's digest body for one held window. The daemon renders it inside the size budget
    and appends the needs-you link.

     Post the supervisor's digest body for one held window. The daemon renders it inside the size budget
    and appends the needs-you link.

    Args:
        body (DigestPostRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DigestPostResponse | EscalationErrorResponse]
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
    body: DigestPostRequest,
) -> DigestPostResponse | EscalationErrorResponse | None:
    """Post the supervisor's digest body for one held window. The daemon renders it inside the size budget
    and appends the needs-you link.

     Post the supervisor's digest body for one held window. The daemon renders it inside the size budget
    and appends the needs-you link.

    Args:
        body (DigestPostRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DigestPostResponse | EscalationErrorResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DigestPostRequest,
) -> Response[DigestPostResponse | EscalationErrorResponse]:
    """Post the supervisor's digest body for one held window. The daemon renders it inside the size budget
    and appends the needs-you link.

     Post the supervisor's digest body for one held window. The daemon renders it inside the size budget
    and appends the needs-you link.

    Args:
        body (DigestPostRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DigestPostResponse | EscalationErrorResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: DigestPostRequest,
) -> DigestPostResponse | EscalationErrorResponse | None:
    """Post the supervisor's digest body for one held window. The daemon renders it inside the size budget
    and appends the needs-you link.

     Post the supervisor's digest body for one held window. The daemon renders it inside the size budget
    and appends the needs-you link.

    Args:
        body (DigestPostRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DigestPostResponse | EscalationErrorResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
