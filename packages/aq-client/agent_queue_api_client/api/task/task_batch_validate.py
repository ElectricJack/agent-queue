from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.task_batch_validate_request import TaskBatchValidateRequest
from ...models.task_batch_validate_response import TaskBatchValidateResponse
from ...models.task_batch_validate_response_422 import TaskBatchValidateResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: TaskBatchValidateRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/batch-validate",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> TaskBatchValidateResponse | TaskBatchValidateResponse422 | None:
    if response.status_code == 200:
        response_200 = TaskBatchValidateResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = TaskBatchValidateResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[TaskBatchValidateResponse | TaskBatchValidateResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskBatchValidateRequest,
) -> Response[TaskBatchValidateResponse | TaskBatchValidateResponse422]:
    """Run the ingestion-batch automatic checks (design §5.1) against the live graph and write nothing.
    Takes the change set task_batch_propose takes, with the ingestion fields (section_anchor,
    primary_outcome, acceptance, size, owned_modules, placement, delivery_targets). Hard findings
    (authority, membership, parent_not_unique, cross_project, depth_exceeded, cycle, container_endpoint,
    external_target_invalid, dependency_on_ancestor, post_landing_inside_delivery_epic,
    delivery_condition_cycle, section_anchor_missing, primary_outcome_missing, acceptance_missing,
    profile_pinned) fail the batch; stale_revision, active_claim and integration_owned fail it with
    retryable: true. Advisory findings (large_leaf, mixed_outcomes, broad_command, duplicate_title,
    redundant_edge, unexplained_serialization, concentrated_ownership) never fail it; gaps name advisory
    checks that lacked declared metadata. Checks are deterministic: no score and no model judgement.

     Run the ingestion-batch automatic checks (design §5.1) against the live graph and write nothing.
    Takes the change set task_batch_propose takes, with the ingestion fields (section_anchor,
    primary_outcome, acceptance, size, owned_modules, placement, delivery_targets). Hard findings
    (authority, membership, parent_not_unique, cross_project, depth_exceeded, cycle, container_endpoint,
    external_target_invalid, dependency_on_ancestor, post_landing_inside_delivery_epic,
    delivery_condition_cycle, section_anchor_missing, primary_outcome_missing, acceptance_missing,
    profile_pinned) fail the batch; stale_revision, active_claim and integration_owned fail it with
    retryable: true. Advisory findings (large_leaf, mixed_outcomes, broad_command, duplicate_title,
    redundant_edge, unexplained_serialization, concentrated_ownership) never fail it; gaps name advisory
    checks that lacked declared metadata. Checks are deterministic: no score and no model judgement.

    Args:
        body (TaskBatchValidateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskBatchValidateResponse | TaskBatchValidateResponse422]
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
    body: TaskBatchValidateRequest,
) -> TaskBatchValidateResponse | TaskBatchValidateResponse422 | None:
    """Run the ingestion-batch automatic checks (design §5.1) against the live graph and write nothing.
    Takes the change set task_batch_propose takes, with the ingestion fields (section_anchor,
    primary_outcome, acceptance, size, owned_modules, placement, delivery_targets). Hard findings
    (authority, membership, parent_not_unique, cross_project, depth_exceeded, cycle, container_endpoint,
    external_target_invalid, dependency_on_ancestor, post_landing_inside_delivery_epic,
    delivery_condition_cycle, section_anchor_missing, primary_outcome_missing, acceptance_missing,
    profile_pinned) fail the batch; stale_revision, active_claim and integration_owned fail it with
    retryable: true. Advisory findings (large_leaf, mixed_outcomes, broad_command, duplicate_title,
    redundant_edge, unexplained_serialization, concentrated_ownership) never fail it; gaps name advisory
    checks that lacked declared metadata. Checks are deterministic: no score and no model judgement.

     Run the ingestion-batch automatic checks (design §5.1) against the live graph and write nothing.
    Takes the change set task_batch_propose takes, with the ingestion fields (section_anchor,
    primary_outcome, acceptance, size, owned_modules, placement, delivery_targets). Hard findings
    (authority, membership, parent_not_unique, cross_project, depth_exceeded, cycle, container_endpoint,
    external_target_invalid, dependency_on_ancestor, post_landing_inside_delivery_epic,
    delivery_condition_cycle, section_anchor_missing, primary_outcome_missing, acceptance_missing,
    profile_pinned) fail the batch; stale_revision, active_claim and integration_owned fail it with
    retryable: true. Advisory findings (large_leaf, mixed_outcomes, broad_command, duplicate_title,
    redundant_edge, unexplained_serialization, concentrated_ownership) never fail it; gaps name advisory
    checks that lacked declared metadata. Checks are deterministic: no score and no model judgement.

    Args:
        body (TaskBatchValidateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskBatchValidateResponse | TaskBatchValidateResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskBatchValidateRequest,
) -> Response[TaskBatchValidateResponse | TaskBatchValidateResponse422]:
    """Run the ingestion-batch automatic checks (design §5.1) against the live graph and write nothing.
    Takes the change set task_batch_propose takes, with the ingestion fields (section_anchor,
    primary_outcome, acceptance, size, owned_modules, placement, delivery_targets). Hard findings
    (authority, membership, parent_not_unique, cross_project, depth_exceeded, cycle, container_endpoint,
    external_target_invalid, dependency_on_ancestor, post_landing_inside_delivery_epic,
    delivery_condition_cycle, section_anchor_missing, primary_outcome_missing, acceptance_missing,
    profile_pinned) fail the batch; stale_revision, active_claim and integration_owned fail it with
    retryable: true. Advisory findings (large_leaf, mixed_outcomes, broad_command, duplicate_title,
    redundant_edge, unexplained_serialization, concentrated_ownership) never fail it; gaps name advisory
    checks that lacked declared metadata. Checks are deterministic: no score and no model judgement.

     Run the ingestion-batch automatic checks (design §5.1) against the live graph and write nothing.
    Takes the change set task_batch_propose takes, with the ingestion fields (section_anchor,
    primary_outcome, acceptance, size, owned_modules, placement, delivery_targets). Hard findings
    (authority, membership, parent_not_unique, cross_project, depth_exceeded, cycle, container_endpoint,
    external_target_invalid, dependency_on_ancestor, post_landing_inside_delivery_epic,
    delivery_condition_cycle, section_anchor_missing, primary_outcome_missing, acceptance_missing,
    profile_pinned) fail the batch; stale_revision, active_claim and integration_owned fail it with
    retryable: true. Advisory findings (large_leaf, mixed_outcomes, broad_command, duplicate_title,
    redundant_edge, unexplained_serialization, concentrated_ownership) never fail it; gaps name advisory
    checks that lacked declared metadata. Checks are deterministic: no score and no model judgement.

    Args:
        body (TaskBatchValidateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskBatchValidateResponse | TaskBatchValidateResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: TaskBatchValidateRequest,
) -> TaskBatchValidateResponse | TaskBatchValidateResponse422 | None:
    """Run the ingestion-batch automatic checks (design §5.1) against the live graph and write nothing.
    Takes the change set task_batch_propose takes, with the ingestion fields (section_anchor,
    primary_outcome, acceptance, size, owned_modules, placement, delivery_targets). Hard findings
    (authority, membership, parent_not_unique, cross_project, depth_exceeded, cycle, container_endpoint,
    external_target_invalid, dependency_on_ancestor, post_landing_inside_delivery_epic,
    delivery_condition_cycle, section_anchor_missing, primary_outcome_missing, acceptance_missing,
    profile_pinned) fail the batch; stale_revision, active_claim and integration_owned fail it with
    retryable: true. Advisory findings (large_leaf, mixed_outcomes, broad_command, duplicate_title,
    redundant_edge, unexplained_serialization, concentrated_ownership) never fail it; gaps name advisory
    checks that lacked declared metadata. Checks are deterministic: no score and no model judgement.

     Run the ingestion-batch automatic checks (design §5.1) against the live graph and write nothing.
    Takes the change set task_batch_propose takes, with the ingestion fields (section_anchor,
    primary_outcome, acceptance, size, owned_modules, placement, delivery_targets). Hard findings
    (authority, membership, parent_not_unique, cross_project, depth_exceeded, cycle, container_endpoint,
    external_target_invalid, dependency_on_ancestor, post_landing_inside_delivery_epic,
    delivery_condition_cycle, section_anchor_missing, primary_outcome_missing, acceptance_missing,
    profile_pinned) fail the batch; stale_revision, active_claim and integration_owned fail it with
    retryable: true. Advisory findings (large_leaf, mixed_outcomes, broad_command, duplicate_title,
    redundant_edge, unexplained_serialization, concentrated_ownership) never fail it; gaps name advisory
    checks that lacked declared metadata. Checks are deterministic: no score and no model judgement.

    Args:
        body (TaskBatchValidateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskBatchValidateResponse | TaskBatchValidateResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
