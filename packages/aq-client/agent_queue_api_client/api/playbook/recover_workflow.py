from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.recover_workflow_request import RecoverWorkflowRequest
from ...models.recover_workflow_response import RecoverWorkflowResponse
from ...models.recover_workflow_response_422 import RecoverWorkflowResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: RecoverWorkflowRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/recover-workflow",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> RecoverWorkflowResponse | RecoverWorkflowResponse422 | None:
    if response.status_code == 200:
        response_200 = RecoverWorkflowResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = RecoverWorkflowResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[RecoverWorkflowResponse | RecoverWorkflowResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: RecoverWorkflowRequest,
) -> Response[RecoverWorkflowResponse | RecoverWorkflowResponse422]:
    """Recover an orphaned coordination workflow whose playbook run has died (crashed, failed, timed out).
    If the playbook was paused waiting for stage completion and all tasks are done, re-emits the missed
    event to resume the playbook. If the playbook run failed, emits a workflow.orphaned event for manual
    intervention. Tasks in the workflow continue executing independently regardless of playbook state.

     Recover an orphaned coordination workflow whose playbook run has died (crashed, failed, timed out).
    If the playbook was paused waiting for stage completion and all tasks are done, re-emits the missed
    event to resume the playbook. If the playbook run failed, emits a workflow.orphaned event for manual
    intervention. Tasks in the workflow continue executing independently regardless of playbook state.

    Args:
        body (RecoverWorkflowRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[RecoverWorkflowResponse | RecoverWorkflowResponse422]
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
    body: RecoverWorkflowRequest,
) -> RecoverWorkflowResponse | RecoverWorkflowResponse422 | None:
    """Recover an orphaned coordination workflow whose playbook run has died (crashed, failed, timed out).
    If the playbook was paused waiting for stage completion and all tasks are done, re-emits the missed
    event to resume the playbook. If the playbook run failed, emits a workflow.orphaned event for manual
    intervention. Tasks in the workflow continue executing independently regardless of playbook state.

     Recover an orphaned coordination workflow whose playbook run has died (crashed, failed, timed out).
    If the playbook was paused waiting for stage completion and all tasks are done, re-emits the missed
    event to resume the playbook. If the playbook run failed, emits a workflow.orphaned event for manual
    intervention. Tasks in the workflow continue executing independently regardless of playbook state.

    Args:
        body (RecoverWorkflowRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        RecoverWorkflowResponse | RecoverWorkflowResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: RecoverWorkflowRequest,
) -> Response[RecoverWorkflowResponse | RecoverWorkflowResponse422]:
    """Recover an orphaned coordination workflow whose playbook run has died (crashed, failed, timed out).
    If the playbook was paused waiting for stage completion and all tasks are done, re-emits the missed
    event to resume the playbook. If the playbook run failed, emits a workflow.orphaned event for manual
    intervention. Tasks in the workflow continue executing independently regardless of playbook state.

     Recover an orphaned coordination workflow whose playbook run has died (crashed, failed, timed out).
    If the playbook was paused waiting for stage completion and all tasks are done, re-emits the missed
    event to resume the playbook. If the playbook run failed, emits a workflow.orphaned event for manual
    intervention. Tasks in the workflow continue executing independently regardless of playbook state.

    Args:
        body (RecoverWorkflowRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[RecoverWorkflowResponse | RecoverWorkflowResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: RecoverWorkflowRequest,
) -> RecoverWorkflowResponse | RecoverWorkflowResponse422 | None:
    """Recover an orphaned coordination workflow whose playbook run has died (crashed, failed, timed out).
    If the playbook was paused waiting for stage completion and all tasks are done, re-emits the missed
    event to resume the playbook. If the playbook run failed, emits a workflow.orphaned event for manual
    intervention. Tasks in the workflow continue executing independently regardless of playbook state.

     Recover an orphaned coordination workflow whose playbook run has died (crashed, failed, timed out).
    If the playbook was paused waiting for stage completion and all tasks are done, re-emits the missed
    event to resume the playbook. If the playbook run failed, emits a workflow.orphaned event for manual
    intervention. Tasks in the workflow continue executing independently regardless of playbook state.

    Args:
        body (RecoverWorkflowRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        RecoverWorkflowResponse | RecoverWorkflowResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
