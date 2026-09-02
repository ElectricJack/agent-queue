from http import HTTPStatus
from typing import Any, cast
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.http_validation_error import HTTPValidationError
from ...models.node_response import NodeResponse
from ...types import UNSET, Response, Unset


def _get_kwargs(
    project_id: str,
    task_id: str,
    *,
    variant: str | Unset = "all",
) -> dict[str, Any]:

    params: dict[str, Any] = {}

    params["variant"] = variant

    params = {k: v for k, v in params.items() if v is not UNSET and v is not None}

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/projects/{project_id}/graph/node/{task_id}".format(
            project_id=quote(str(project_id), safe=""),
            task_id=quote(str(task_id), safe=""),
        ),
        "params": params,
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Any | HTTPValidationError | NodeResponse | None:
    if response.status_code == 200:
        response_200 = NodeResponse.from_dict(response.json())

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
) -> Response[Any | HTTPValidationError | NodeResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    project_id: str,
    task_id: str,
    *,
    client: AuthenticatedClient | Client,
    variant: str | Unset = "all",
) -> Response[Any | HTTPValidationError | NodeResponse]:
    """Get Node

    Args:
        project_id (str):
        task_id (str):
        variant (str | Unset):  Default: 'all'.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | HTTPValidationError | NodeResponse]
    """

    kwargs = _get_kwargs(
        project_id=project_id,
        task_id=task_id,
        variant=variant,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    project_id: str,
    task_id: str,
    *,
    client: AuthenticatedClient | Client,
    variant: str | Unset = "all",
) -> Any | HTTPValidationError | NodeResponse | None:
    """Get Node

    Args:
        project_id (str):
        task_id (str):
        variant (str | Unset):  Default: 'all'.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | HTTPValidationError | NodeResponse
    """

    return sync_detailed(
        project_id=project_id,
        task_id=task_id,
        client=client,
        variant=variant,
    ).parsed


async def asyncio_detailed(
    project_id: str,
    task_id: str,
    *,
    client: AuthenticatedClient | Client,
    variant: str | Unset = "all",
) -> Response[Any | HTTPValidationError | NodeResponse]:
    """Get Node

    Args:
        project_id (str):
        task_id (str):
        variant (str | Unset):  Default: 'all'.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[Any | HTTPValidationError | NodeResponse]
    """

    kwargs = _get_kwargs(
        project_id=project_id,
        task_id=task_id,
        variant=variant,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    project_id: str,
    task_id: str,
    *,
    client: AuthenticatedClient | Client,
    variant: str | Unset = "all",
) -> Any | HTTPValidationError | NodeResponse | None:
    """Get Node

    Args:
        project_id (str):
        task_id (str):
        variant (str | Unset):  Default: 'all'.

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Any | HTTPValidationError | NodeResponse
    """

    return (
        await asyncio_detailed(
            project_id=project_id,
            task_id=task_id,
            client=client,
            variant=variant,
        )
    ).parsed
