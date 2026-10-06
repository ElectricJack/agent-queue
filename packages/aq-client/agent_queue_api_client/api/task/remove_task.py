from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.hierarchy_refusal_response import HierarchyRefusalResponse
from ...models.remove_task_request import RemoveTaskRequest
from ...models.remove_task_response import RemoveTaskResponse
from ...types import Response


def _get_kwargs(
    *,
    body: RemoveTaskRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/remove",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> HierarchyRefusalResponse | RemoveTaskResponse | None:
    if response.status_code == 200:
        response_200 = RemoveTaskResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = HierarchyRefusalResponse.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[HierarchyRefusalResponse | RemoveTaskResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: RemoveTaskRequest,
) -> Response[HierarchyRefusalResponse | RemoveTaskResponse]:
    """Remove a task or epic subtree. Operator/live supervisor only. With confirmed=false, preview without
    changing anything. One confirmed decision stops sessions, aborts open batches, cancels ownerless
    legacy operations and archives when audit history exists. Branches are kept. Requires a reason when
    confirmed.

     Remove a task or epic subtree. Operator/live supervisor only. With confirmed=false, preview without
    changing anything. One confirmed decision stops sessions, aborts open batches, cancels ownerless
    legacy operations and archives when audit history exists. Branches are kept. Requires a reason when
    confirmed.

    Args:
        body (RemoveTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HierarchyRefusalResponse | RemoveTaskResponse]
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
    body: RemoveTaskRequest,
) -> HierarchyRefusalResponse | RemoveTaskResponse | None:
    """Remove a task or epic subtree. Operator/live supervisor only. With confirmed=false, preview without
    changing anything. One confirmed decision stops sessions, aborts open batches, cancels ownerless
    legacy operations and archives when audit history exists. Branches are kept. Requires a reason when
    confirmed.

     Remove a task or epic subtree. Operator/live supervisor only. With confirmed=false, preview without
    changing anything. One confirmed decision stops sessions, aborts open batches, cancels ownerless
    legacy operations and archives when audit history exists. Branches are kept. Requires a reason when
    confirmed.

    Args:
        body (RemoveTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HierarchyRefusalResponse | RemoveTaskResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: RemoveTaskRequest,
) -> Response[HierarchyRefusalResponse | RemoveTaskResponse]:
    """Remove a task or epic subtree. Operator/live supervisor only. With confirmed=false, preview without
    changing anything. One confirmed decision stops sessions, aborts open batches, cancels ownerless
    legacy operations and archives when audit history exists. Branches are kept. Requires a reason when
    confirmed.

     Remove a task or epic subtree. Operator/live supervisor only. With confirmed=false, preview without
    changing anything. One confirmed decision stops sessions, aborts open batches, cancels ownerless
    legacy operations and archives when audit history exists. Branches are kept. Requires a reason when
    confirmed.

    Args:
        body (RemoveTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HierarchyRefusalResponse | RemoveTaskResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: RemoveTaskRequest,
) -> HierarchyRefusalResponse | RemoveTaskResponse | None:
    """Remove a task or epic subtree. Operator/live supervisor only. With confirmed=false, preview without
    changing anything. One confirmed decision stops sessions, aborts open batches, cancels ownerless
    legacy operations and archives when audit history exists. Branches are kept. Requires a reason when
    confirmed.

     Remove a task or epic subtree. Operator/live supervisor only. With confirmed=false, preview without
    changing anything. One confirmed decision stops sessions, aborts open batches, cancels ownerless
    legacy operations and archives when audit history exists. Branches are kept. Requires a reason when
    confirmed.

    Args:
        body (RemoveTaskRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HierarchyRefusalResponse | RemoveTaskResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
