from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_graph_layout_save_request import PlaybookGraphLayoutSaveRequest
from ...models.playbook_graph_layout_save_response import PlaybookGraphLayoutSaveResponse
from ...models.playbook_graph_layout_save_response_422 import PlaybookGraphLayoutSaveResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookGraphLayoutSaveRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/graph-layout-save",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookGraphLayoutSaveResponse | PlaybookGraphLayoutSaveResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookGraphLayoutSaveResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookGraphLayoutSaveResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookGraphLayoutSaveResponse | PlaybookGraphLayoutSaveResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookGraphLayoutSaveRequest,
) -> Response[PlaybookGraphLayoutSaveResponse | PlaybookGraphLayoutSaveResponse422]:
    """Persist user-arranged grid coordinates for the nodes of one immutable playbook artifact. A later
    compile creates a new artifact and therefore starts with compiler-generated coordinates.

     Persist user-arranged grid coordinates for the nodes of one immutable playbook artifact. A later
    compile creates a new artifact and therefore starts with compiler-generated coordinates.

    Args:
        body (PlaybookGraphLayoutSaveRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookGraphLayoutSaveResponse | PlaybookGraphLayoutSaveResponse422]
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
    body: PlaybookGraphLayoutSaveRequest,
) -> PlaybookGraphLayoutSaveResponse | PlaybookGraphLayoutSaveResponse422 | None:
    """Persist user-arranged grid coordinates for the nodes of one immutable playbook artifact. A later
    compile creates a new artifact and therefore starts with compiler-generated coordinates.

     Persist user-arranged grid coordinates for the nodes of one immutable playbook artifact. A later
    compile creates a new artifact and therefore starts with compiler-generated coordinates.

    Args:
        body (PlaybookGraphLayoutSaveRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookGraphLayoutSaveResponse | PlaybookGraphLayoutSaveResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookGraphLayoutSaveRequest,
) -> Response[PlaybookGraphLayoutSaveResponse | PlaybookGraphLayoutSaveResponse422]:
    """Persist user-arranged grid coordinates for the nodes of one immutable playbook artifact. A later
    compile creates a new artifact and therefore starts with compiler-generated coordinates.

     Persist user-arranged grid coordinates for the nodes of one immutable playbook artifact. A later
    compile creates a new artifact and therefore starts with compiler-generated coordinates.

    Args:
        body (PlaybookGraphLayoutSaveRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookGraphLayoutSaveResponse | PlaybookGraphLayoutSaveResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookGraphLayoutSaveRequest,
) -> PlaybookGraphLayoutSaveResponse | PlaybookGraphLayoutSaveResponse422 | None:
    """Persist user-arranged grid coordinates for the nodes of one immutable playbook artifact. A later
    compile creates a new artifact and therefore starts with compiler-generated coordinates.

     Persist user-arranged grid coordinates for the nodes of one immutable playbook artifact. A later
    compile creates a new artifact and therefore starts with compiler-generated coordinates.

    Args:
        body (PlaybookGraphLayoutSaveRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookGraphLayoutSaveResponse | PlaybookGraphLayoutSaveResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
