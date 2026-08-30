from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.list_playbooks_request import ListPlaybooksRequest
from ...models.list_playbooks_response import ListPlaybooksResponse
from ...models.list_playbooks_response_422 import ListPlaybooksResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ListPlaybooksRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/list",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ListPlaybooksResponse | ListPlaybooksResponse422 | None:
    if response.status_code == 200:
        response_200 = ListPlaybooksResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ListPlaybooksResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ListPlaybooksResponse | ListPlaybooksResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListPlaybooksRequest,
) -> Response[ListPlaybooksResponse | ListPlaybooksResponse422]:
    """List all playbooks across scopes with status, triggers, and last run info. Returns every active
    compiled playbook. Optionally filter by scope. When project_id is provided, project-scoped playbooks
    belonging to a different project are excluded; system and agent-type scoped playbooks are always
    included because they apply across projects.

     List all playbooks across scopes with status, triggers, and last run info. Returns every active
    compiled playbook. Optionally filter by scope. When project_id is provided, project-scoped playbooks
    belonging to a different project are excluded; system and agent-type scoped playbooks are always
    included because they apply across projects.

    Args:
        body (ListPlaybooksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListPlaybooksResponse | ListPlaybooksResponse422]
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
    body: ListPlaybooksRequest,
) -> ListPlaybooksResponse | ListPlaybooksResponse422 | None:
    """List all playbooks across scopes with status, triggers, and last run info. Returns every active
    compiled playbook. Optionally filter by scope. When project_id is provided, project-scoped playbooks
    belonging to a different project are excluded; system and agent-type scoped playbooks are always
    included because they apply across projects.

     List all playbooks across scopes with status, triggers, and last run info. Returns every active
    compiled playbook. Optionally filter by scope. When project_id is provided, project-scoped playbooks
    belonging to a different project are excluded; system and agent-type scoped playbooks are always
    included because they apply across projects.

    Args:
        body (ListPlaybooksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListPlaybooksResponse | ListPlaybooksResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListPlaybooksRequest,
) -> Response[ListPlaybooksResponse | ListPlaybooksResponse422]:
    """List all playbooks across scopes with status, triggers, and last run info. Returns every active
    compiled playbook. Optionally filter by scope. When project_id is provided, project-scoped playbooks
    belonging to a different project are excluded; system and agent-type scoped playbooks are always
    included because they apply across projects.

     List all playbooks across scopes with status, triggers, and last run info. Returns every active
    compiled playbook. Optionally filter by scope. When project_id is provided, project-scoped playbooks
    belonging to a different project are excluded; system and agent-type scoped playbooks are always
    included because they apply across projects.

    Args:
        body (ListPlaybooksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListPlaybooksResponse | ListPlaybooksResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ListPlaybooksRequest,
) -> ListPlaybooksResponse | ListPlaybooksResponse422 | None:
    """List all playbooks across scopes with status, triggers, and last run info. Returns every active
    compiled playbook. Optionally filter by scope. When project_id is provided, project-scoped playbooks
    belonging to a different project are excluded; system and agent-type scoped playbooks are always
    included because they apply across projects.

     List all playbooks across scopes with status, triggers, and last run info. Returns every active
    compiled playbook. Optionally filter by scope. When project_id is provided, project-scoped playbooks
    belonging to a different project are excluded; system and agent-type scoped playbooks are always
    included because they apply across projects.

    Args:
        body (ListPlaybooksRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListPlaybooksResponse | ListPlaybooksResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
