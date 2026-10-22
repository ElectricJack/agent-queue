from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.project_relocate_request import ProjectRelocateRequest
from ...models.project_relocate_response_422 import ProjectRelocateResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ProjectRelocateRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/project/relocate",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | ProjectRelocateResponse422 | None:
    if response.status_code == 200:
        response_200 = response.json()
        return response_200

    if response.status_code == 422:
        response_422 = ProjectRelocateResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[Any | ProjectRelocateResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProjectRelocateRequest,
) -> Response[Any | ProjectRelocateResponse422]:
    """Preview or apply an atomic project storage relocation. Requires paused admission, disabled/drained
    train, stopped writers and current required CI. Reverse relocation uses a fresh destination
    containing current work. Local operator only.

     Preview or apply an atomic project storage relocation. Requires paused admission, disabled/drained
    train, stopped writers and current required CI. Reverse relocation uses a fresh destination
    containing current work. Local operator only.

    Args:
        body (ProjectRelocateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ProjectRelocateResponse422]
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
    body: ProjectRelocateRequest,
) -> Any | ProjectRelocateResponse422 | None:
    """Preview or apply an atomic project storage relocation. Requires paused admission, disabled/drained
    train, stopped writers and current required CI. Reverse relocation uses a fresh destination
    containing current work. Local operator only.

     Preview or apply an atomic project storage relocation. Requires paused admission, disabled/drained
    train, stopped writers and current required CI. Reverse relocation uses a fresh destination
    containing current work. Local operator only.

    Args:
        body (ProjectRelocateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ProjectRelocateResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProjectRelocateRequest,
) -> Response[Any | ProjectRelocateResponse422]:
    """Preview or apply an atomic project storage relocation. Requires paused admission, disabled/drained
    train, stopped writers and current required CI. Reverse relocation uses a fresh destination
    containing current work. Local operator only.

     Preview or apply an atomic project storage relocation. Requires paused admission, disabled/drained
    train, stopped writers and current required CI. Reverse relocation uses a fresh destination
    containing current work. Local operator only.

    Args:
        body (ProjectRelocateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | ProjectRelocateResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ProjectRelocateRequest,
) -> Any | ProjectRelocateResponse422 | None:
    """Preview or apply an atomic project storage relocation. Requires paused admission, disabled/drained
    train, stopped writers and current required CI. Reverse relocation uses a fresh destination
    containing current work. Local operator only.

     Preview or apply an atomic project storage relocation. Requires paused admission, disabled/drained
    train, stopped writers and current required CI. Reverse relocation uses a fresh destination
    containing current work. Local operator only.

    Args:
        body (ProjectRelocateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | ProjectRelocateResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
