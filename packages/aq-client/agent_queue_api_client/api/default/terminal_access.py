from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.http_validation_error import HTTPValidationError
from ...models.terminal_access_response import TerminalAccessResponse
from ...types import UNSET, Response, Unset


def _get_kwargs(
    session_id: str,
    *,
    browser_origin: None | str | Unset = UNSET,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_browser_origin: None | str | Unset
    if isinstance(browser_origin, Unset):
        json_browser_origin = UNSET
    else:
        json_browser_origin = browser_origin
    params["browser_origin"] = json_browser_origin

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/ws/terminal/{session_id}".format(
            session_id=quote(str(session_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> HTTPValidationError | TerminalAccessResponse | None:
    if response.status_code == 200:
        response_200 = TerminalAccessResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[HTTPValidationError | TerminalAccessResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    session_id: str,
    *,
    client: AuthenticatedClient | Client,
    browser_origin: None | str | Unset = UNSET,
) -> Response[HTTPValidationError | TerminalAccessResponse]:
    """Terminal Access

    Args:
        session_id (str):
        browser_origin (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | TerminalAccessResponse]
    """

    kwargs = _get_kwargs(
        session_id=session_id,
        browser_origin=browser_origin,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    session_id: str,
    *,
    client: AuthenticatedClient | Client,
    browser_origin: None | str | Unset = UNSET,
) -> HTTPValidationError | TerminalAccessResponse | None:
    """Terminal Access

    Args:
        session_id (str):
        browser_origin (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | TerminalAccessResponse
    """

    return sync_detailed(
        session_id=session_id,
        client=client,
        browser_origin=browser_origin,
    ).parsed


async def asyncio_detailed(
    session_id: str,
    *,
    client: AuthenticatedClient | Client,
    browser_origin: None | str | Unset = UNSET,
) -> Response[HTTPValidationError | TerminalAccessResponse]:
    """Terminal Access

    Args:
        session_id (str):
        browser_origin (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | TerminalAccessResponse]
    """

    kwargs = _get_kwargs(
        session_id=session_id,
        browser_origin=browser_origin,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    session_id: str,
    *,
    client: AuthenticatedClient | Client,
    browser_origin: None | str | Unset = UNSET,
) -> HTTPValidationError | TerminalAccessResponse | None:
    """Terminal Access

    Args:
        session_id (str):
        browser_origin (None | str | Unset):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | TerminalAccessResponse
    """

    return (
        await asyncio_detailed(
            session_id=session_id,
            client=client,
            browser_origin=browser_origin,
        )
    ).parsed
