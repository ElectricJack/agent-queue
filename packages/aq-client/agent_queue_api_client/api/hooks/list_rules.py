from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.browse_rules_response import BrowseRulesResponse
from ...models.list_rules_request import ListRulesRequest
from ...models.list_rules_response_422 import ListRulesResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ListRulesRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/hooks/list-rules",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> BrowseRulesResponse | ListRulesResponse422 | None:
    if response.status_code == 200:
        response_200 = BrowseRulesResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ListRulesResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[BrowseRulesResponse | ListRulesResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListRulesRequest,
) -> Response[BrowseRulesResponse | ListRulesResponse422]:
    """List all automation rules for the current project and globals. Rules are the ONLY way to create
    automation — each active rule generates hooks that execute automatically. Alias: browse_rules

     List all automation rules for the current project and globals. Rules are the ONLY way to create
    automation — each active rule generates hooks that execute automatically. Alias: browse_rules

    Args:
        body (ListRulesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BrowseRulesResponse | ListRulesResponse422]
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
    body: ListRulesRequest,
) -> BrowseRulesResponse | ListRulesResponse422 | None:
    """List all automation rules for the current project and globals. Rules are the ONLY way to create
    automation — each active rule generates hooks that execute automatically. Alias: browse_rules

     List all automation rules for the current project and globals. Rules are the ONLY way to create
    automation — each active rule generates hooks that execute automatically. Alias: browse_rules

    Args:
        body (ListRulesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BrowseRulesResponse | ListRulesResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ListRulesRequest,
) -> Response[BrowseRulesResponse | ListRulesResponse422]:
    """List all automation rules for the current project and globals. Rules are the ONLY way to create
    automation — each active rule generates hooks that execute automatically. Alias: browse_rules

     List all automation rules for the current project and globals. Rules are the ONLY way to create
    automation — each active rule generates hooks that execute automatically. Alias: browse_rules

    Args:
        body (ListRulesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[BrowseRulesResponse | ListRulesResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ListRulesRequest,
) -> BrowseRulesResponse | ListRulesResponse422 | None:
    """List all automation rules for the current project and globals. Rules are the ONLY way to create
    automation — each active rule generates hooks that execute automatically. Alias: browse_rules

     List all automation rules for the current project and globals. Rules are the ONLY way to create
    automation — each active rule generates hooks that execute automatically. Alias: browse_rules

    Args:
        body (ListRulesRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        BrowseRulesResponse | ListRulesResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
