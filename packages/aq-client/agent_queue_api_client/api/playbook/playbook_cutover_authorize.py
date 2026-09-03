from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_cutover_authorize_request import PlaybookCutoverAuthorizeRequest
from ...models.playbook_cutover_authorize_response import PlaybookCutoverAuthorizeResponse
from ...models.playbook_cutover_authorize_response_422 import PlaybookCutoverAuthorizeResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookCutoverAuthorizeRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/cutover-authorize",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookCutoverAuthorizeResponse | PlaybookCutoverAuthorizeResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookCutoverAuthorizeResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookCutoverAuthorizeResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookCutoverAuthorizeResponse | PlaybookCutoverAuthorizeResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookCutoverAuthorizeRequest,
) -> Response[PlaybookCutoverAuthorizeResponse | PlaybookCutoverAuthorizeResponse422]:
    """Gate G2: one named human authorizes the switch to v2 in one role, author or release_operator.
    Operator-only. Bound to the current G1 drain sign-off and refused without one; one signature per
    role and one role per person, so the switch needs two different people. Appends a cutover_authorized
    audit row.

     Gate G2: one named human authorizes the switch to v2 in one role, author or release_operator.
    Operator-only. Bound to the current G1 drain sign-off and refused without one; one signature per
    role and one role per person, so the switch needs two different people. Appends a cutover_authorized
    audit row.

    Args:
        body (PlaybookCutoverAuthorizeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookCutoverAuthorizeResponse | PlaybookCutoverAuthorizeResponse422]
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
    body: PlaybookCutoverAuthorizeRequest,
) -> PlaybookCutoverAuthorizeResponse | PlaybookCutoverAuthorizeResponse422 | None:
    """Gate G2: one named human authorizes the switch to v2 in one role, author or release_operator.
    Operator-only. Bound to the current G1 drain sign-off and refused without one; one signature per
    role and one role per person, so the switch needs two different people. Appends a cutover_authorized
    audit row.

     Gate G2: one named human authorizes the switch to v2 in one role, author or release_operator.
    Operator-only. Bound to the current G1 drain sign-off and refused without one; one signature per
    role and one role per person, so the switch needs two different people. Appends a cutover_authorized
    audit row.

    Args:
        body (PlaybookCutoverAuthorizeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookCutoverAuthorizeResponse | PlaybookCutoverAuthorizeResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookCutoverAuthorizeRequest,
) -> Response[PlaybookCutoverAuthorizeResponse | PlaybookCutoverAuthorizeResponse422]:
    """Gate G2: one named human authorizes the switch to v2 in one role, author or release_operator.
    Operator-only. Bound to the current G1 drain sign-off and refused without one; one signature per
    role and one role per person, so the switch needs two different people. Appends a cutover_authorized
    audit row.

     Gate G2: one named human authorizes the switch to v2 in one role, author or release_operator.
    Operator-only. Bound to the current G1 drain sign-off and refused without one; one signature per
    role and one role per person, so the switch needs two different people. Appends a cutover_authorized
    audit row.

    Args:
        body (PlaybookCutoverAuthorizeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookCutoverAuthorizeResponse | PlaybookCutoverAuthorizeResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookCutoverAuthorizeRequest,
) -> PlaybookCutoverAuthorizeResponse | PlaybookCutoverAuthorizeResponse422 | None:
    """Gate G2: one named human authorizes the switch to v2 in one role, author or release_operator.
    Operator-only. Bound to the current G1 drain sign-off and refused without one; one signature per
    role and one role per person, so the switch needs two different people. Appends a cutover_authorized
    audit row.

     Gate G2: one named human authorizes the switch to v2 in one role, author or release_operator.
    Operator-only. Bound to the current G1 drain sign-off and refused without one; one signature per
    role and one role per person, so the switch needs two different people. Appends a cutover_authorized
    audit row.

    Args:
        body (PlaybookCutoverAuthorizeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookCutoverAuthorizeResponse | PlaybookCutoverAuthorizeResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
