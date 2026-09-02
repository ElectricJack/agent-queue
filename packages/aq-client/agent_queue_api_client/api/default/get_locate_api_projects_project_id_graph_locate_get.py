from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.http_validation_error import HTTPValidationError
from ...models.locate_response import LocateResponse
from ...types import UNSET, Response, Unset


def _get_kwargs(
    project_id: str,
    *,
    variant: str | Unset = "active",
    q: str | Unset = "",
    status: str | Unset = "",
    limit: int | Unset = 200,
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["variant"] = variant

    params["q"] = q

    params["status"] = status

    params["limit"] = limit

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/projects/{project_id}/graph/locate".format(
            project_id=quote(str(project_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | HTTPValidationError | LocateResponse | None:
    if response.status_code == 200:
        response_200 = LocateResponse.from_dict(response.json())

        return response_200

    if response.status_code == 202:
        response_202 = cast(Any, None)
        return response_202

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Any | HTTPValidationError | LocateResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    project_id: str,
    *,
    client: AuthenticatedClient | Client,
    variant: str | Unset = "active",
    q: str | Unset = "",
    status: str | Unset = "",
    limit: int | Unset = 200,
) -> Response[Any | HTTPValidationError | LocateResponse]:
    """Get Locate

    Args:
        project_id (str):
        variant (str | Unset):  Default: 'active'.
        q (str | Unset):  Default: ''.
        status (str | Unset):  Default: ''.
        limit (int | Unset):  Default: 200.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | HTTPValidationError | LocateResponse]
    """

    kwargs = _get_kwargs(
        project_id=project_id,
        variant=variant,
        q=q,
        status=status,
        limit=limit,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    project_id: str,
    *,
    client: AuthenticatedClient | Client,
    variant: str | Unset = "active",
    q: str | Unset = "",
    status: str | Unset = "",
    limit: int | Unset = 200,
) -> Any | HTTPValidationError | LocateResponse | None:
    """Get Locate

    Args:
        project_id (str):
        variant (str | Unset):  Default: 'active'.
        q (str | Unset):  Default: ''.
        status (str | Unset):  Default: ''.
        limit (int | Unset):  Default: 200.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | HTTPValidationError | LocateResponse
    """

    return sync_detailed(
        project_id=project_id,
        client=client,
        variant=variant,
        q=q,
        status=status,
        limit=limit,
    ).parsed


async def asyncio_detailed(
    project_id: str,
    *,
    client: AuthenticatedClient | Client,
    variant: str | Unset = "active",
    q: str | Unset = "",
    status: str | Unset = "",
    limit: int | Unset = 200,
) -> Response[Any | HTTPValidationError | LocateResponse]:
    """Get Locate

    Args:
        project_id (str):
        variant (str | Unset):  Default: 'active'.
        q (str | Unset):  Default: ''.
        status (str | Unset):  Default: ''.
        limit (int | Unset):  Default: 200.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | HTTPValidationError | LocateResponse]
    """

    kwargs = _get_kwargs(
        project_id=project_id,
        variant=variant,
        q=q,
        status=status,
        limit=limit,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    project_id: str,
    *,
    client: AuthenticatedClient | Client,
    variant: str | Unset = "active",
    q: str | Unset = "",
    status: str | Unset = "",
    limit: int | Unset = 200,
) -> Any | HTTPValidationError | LocateResponse | None:
    """Get Locate

    Args:
        project_id (str):
        variant (str | Unset):  Default: 'active'.
        q (str | Unset):  Default: ''.
        status (str | Unset):  Default: ''.
        limit (int | Unset):  Default: 200.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | HTTPValidationError | LocateResponse
    """

    return (
        await asyncio_detailed(
            project_id=project_id,
            client=client,
            variant=variant,
            q=q,
            status=status,
            limit=limit,
        )
    ).parsed
