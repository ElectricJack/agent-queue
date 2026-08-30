from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.http_validation_error import HTTPValidationError
from ...types import UNSET, Response, Unset


def _get_kwargs(
    session_id: str,
    *,
    max_seconds: float | None | Unset = UNSET,
    replay_only: int | Unset = 0,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    json_max_seconds: float | None | Unset
    if isinstance(max_seconds, Unset):
        json_max_seconds = UNSET
    else:
        json_max_seconds = max_seconds
    params["max_seconds"] = json_max_seconds

    params["replay_only"] = replay_only

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/sessions/{session_id}/stream".format(
            session_id=quote(str(session_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | HTTPValidationError | None:
    if response.status_code == 200:
        response_200 = response.json()
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
) -> Response[Any | HTTPValidationError]:
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
    max_seconds: float | None | Unset = UNSET,
    replay_only: int | Unset = 0,
) -> Response[Any | HTTPValidationError]:
    """Stream

    Args:
        session_id (str):
        max_seconds (float | None | Unset):
        replay_only (int | Unset):  Default: 0.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | HTTPValidationError]
    """

    kwargs = _get_kwargs(
        session_id=session_id,
        max_seconds=max_seconds,
        replay_only=replay_only,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    session_id: str,
    *,
    client: AuthenticatedClient | Client,
    max_seconds: float | None | Unset = UNSET,
    replay_only: int | Unset = 0,
) -> Any | HTTPValidationError | None:
    """Stream

    Args:
        session_id (str):
        max_seconds (float | None | Unset):
        replay_only (int | Unset):  Default: 0.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | HTTPValidationError
    """

    return sync_detailed(
        session_id=session_id,
        client=client,
        max_seconds=max_seconds,
        replay_only=replay_only,
    ).parsed


async def asyncio_detailed(
    session_id: str,
    *,
    client: AuthenticatedClient | Client,
    max_seconds: float | None | Unset = UNSET,
    replay_only: int | Unset = 0,
) -> Response[Any | HTTPValidationError]:
    """Stream

    Args:
        session_id (str):
        max_seconds (float | None | Unset):
        replay_only (int | Unset):  Default: 0.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | HTTPValidationError]
    """

    kwargs = _get_kwargs(
        session_id=session_id,
        max_seconds=max_seconds,
        replay_only=replay_only,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    session_id: str,
    *,
    client: AuthenticatedClient | Client,
    max_seconds: float | None | Unset = UNSET,
    replay_only: int | Unset = 0,
) -> Any | HTTPValidationError | None:
    """Stream

    Args:
        session_id (str):
        max_seconds (float | None | Unset):
        replay_only (int | Unset):  Default: 0.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | HTTPValidationError
    """

    return (
        await asyncio_detailed(
            session_id=session_id,
            client=client,
            max_seconds=max_seconds,
            replay_only=replay_only,
        )
    ).parsed
