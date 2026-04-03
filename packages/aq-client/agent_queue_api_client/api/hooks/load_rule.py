from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.load_rule_request import LoadRuleRequest
from ...models.load_rule_response_422 import LoadRuleResponse422
from ...models.rule_operation_response import RuleOperationResponse
from ...types import Response


def _get_kwargs(
    *,
    body: LoadRuleRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/hooks/load-rule",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> LoadRuleResponse422 | RuleOperationResponse | None:
    if response.status_code == 200:
        response_200 = RuleOperationResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = LoadRuleResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[LoadRuleResponse422 | RuleOperationResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: LoadRuleRequest,
) -> Response[LoadRuleResponse422 | RuleOperationResponse]:
    """Load a specific rule's full content and metadata, including its generated hook IDs.

     Load a specific rule's full content and metadata, including its generated hook IDs.

    Args:
        body (LoadRuleRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[LoadRuleResponse422 | RuleOperationResponse]
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
    body: LoadRuleRequest,
) -> LoadRuleResponse422 | RuleOperationResponse | None:
    """Load a specific rule's full content and metadata, including its generated hook IDs.

     Load a specific rule's full content and metadata, including its generated hook IDs.

    Args:
        body (LoadRuleRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        LoadRuleResponse422 | RuleOperationResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: LoadRuleRequest,
) -> Response[LoadRuleResponse422 | RuleOperationResponse]:
    """Load a specific rule's full content and metadata, including its generated hook IDs.

     Load a specific rule's full content and metadata, including its generated hook IDs.

    Args:
        body (LoadRuleRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[LoadRuleResponse422 | RuleOperationResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: LoadRuleRequest,
) -> LoadRuleResponse422 | RuleOperationResponse | None:
    """Load a specific rule's full content and metadata, including its generated hook IDs.

     Load a specific rule's full content and metadata, including its generated hook IDs.

    Args:
        body (LoadRuleRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        LoadRuleResponse422 | RuleOperationResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
