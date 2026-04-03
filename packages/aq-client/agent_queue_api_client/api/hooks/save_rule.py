from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.rule_operation_response import RuleOperationResponse
from ...models.save_rule_request import SaveRuleRequest
from ...models.save_rule_response_422 import SaveRuleResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: SaveRuleRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/hooks/save-rule",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> RuleOperationResponse | SaveRuleResponse422 | None:
    if response.status_code == 200:
        response_200 = RuleOperationResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = SaveRuleResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[RuleOperationResponse | SaveRuleResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SaveRuleRequest,
) -> Response[RuleOperationResponse | SaveRuleResponse422]:
    """Create or update an automation rule. This is the ONLY way to create automation — never create hooks
    directly. Active rules with triggers automatically generate hooks that execute on schedule or in
    response to events. Passive rules influence reasoning without triggering actions. Include a # Title,
    ## Trigger (e.g. 'Check every 5 minutes' or 'When a task is completed'), and ## Logic section in the
    content.

     Create or update an automation rule. This is the ONLY way to create automation — never create hooks
    directly. Active rules with triggers automatically generate hooks that execute on schedule or in
    response to events. Passive rules influence reasoning without triggering actions. Include a # Title,
    ## Trigger (e.g. 'Check every 5 minutes' or 'When a task is completed'), and ## Logic section in the
    content.

    Args:
        body (SaveRuleRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[RuleOperationResponse | SaveRuleResponse422]
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
    body: SaveRuleRequest,
) -> RuleOperationResponse | SaveRuleResponse422 | None:
    """Create or update an automation rule. This is the ONLY way to create automation — never create hooks
    directly. Active rules with triggers automatically generate hooks that execute on schedule or in
    response to events. Passive rules influence reasoning without triggering actions. Include a # Title,
    ## Trigger (e.g. 'Check every 5 minutes' or 'When a task is completed'), and ## Logic section in the
    content.

     Create or update an automation rule. This is the ONLY way to create automation — never create hooks
    directly. Active rules with triggers automatically generate hooks that execute on schedule or in
    response to events. Passive rules influence reasoning without triggering actions. Include a # Title,
    ## Trigger (e.g. 'Check every 5 minutes' or 'When a task is completed'), and ## Logic section in the
    content.

    Args:
        body (SaveRuleRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        RuleOperationResponse | SaveRuleResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SaveRuleRequest,
) -> Response[RuleOperationResponse | SaveRuleResponse422]:
    """Create or update an automation rule. This is the ONLY way to create automation — never create hooks
    directly. Active rules with triggers automatically generate hooks that execute on schedule or in
    response to events. Passive rules influence reasoning without triggering actions. Include a # Title,
    ## Trigger (e.g. 'Check every 5 minutes' or 'When a task is completed'), and ## Logic section in the
    content.

     Create or update an automation rule. This is the ONLY way to create automation — never create hooks
    directly. Active rules with triggers automatically generate hooks that execute on schedule or in
    response to events. Passive rules influence reasoning without triggering actions. Include a # Title,
    ## Trigger (e.g. 'Check every 5 minutes' or 'When a task is completed'), and ## Logic section in the
    content.

    Args:
        body (SaveRuleRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[RuleOperationResponse | SaveRuleResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: SaveRuleRequest,
) -> RuleOperationResponse | SaveRuleResponse422 | None:
    """Create or update an automation rule. This is the ONLY way to create automation — never create hooks
    directly. Active rules with triggers automatically generate hooks that execute on schedule or in
    response to events. Passive rules influence reasoning without triggering actions. Include a # Title,
    ## Trigger (e.g. 'Check every 5 minutes' or 'When a task is completed'), and ## Logic section in the
    content.

     Create or update an automation rule. This is the ONLY way to create automation — never create hooks
    directly. Active rules with triggers automatically generate hooks that execute on schedule or in
    response to events. Passive rules influence reasoning without triggering actions. Include a # Title,
    ## Trigger (e.g. 'Check every 5 minutes' or 'When a task is completed'), and ## Logic section in the
    content.

    Args:
        body (SaveRuleRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        RuleOperationResponse | SaveRuleResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
