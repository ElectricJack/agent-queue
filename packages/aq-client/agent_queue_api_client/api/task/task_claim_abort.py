from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.task_claim_abort_request import TaskClaimAbortRequest
from ...models.task_claim_abort_response import TaskClaimAbortResponse
from ...models.task_claim_abort_response_422 import TaskClaimAbortResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: TaskClaimAbortRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/task/claim-abort",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> TaskClaimAbortResponse | TaskClaimAbortResponse422 | None:
    if response.status_code == 200:
        response_200 = TaskClaimAbortResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = TaskClaimAbortResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[TaskClaimAbortResponse | TaskClaimAbortResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskClaimAbortRequest,
) -> Response[TaskClaimAbortResponse | TaskClaimAbortResponse422]:
    """Abort one exact hung pool claim preparation (operator or live supervisor only). Waits up to ten
    seconds for owned Git/SQL cleanup. Unproved cleanup retains the claim and branch ownership; active
    workers are refused.

     Abort one exact hung pool claim preparation (operator or live supervisor only). Waits up to ten
    seconds for owned Git/SQL cleanup. Unproved cleanup retains the claim and branch ownership; active
    workers are refused.

    Args:
        body (TaskClaimAbortRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskClaimAbortResponse | TaskClaimAbortResponse422]
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
    body: TaskClaimAbortRequest,
) -> TaskClaimAbortResponse | TaskClaimAbortResponse422 | None:
    """Abort one exact hung pool claim preparation (operator or live supervisor only). Waits up to ten
    seconds for owned Git/SQL cleanup. Unproved cleanup retains the claim and branch ownership; active
    workers are refused.

     Abort one exact hung pool claim preparation (operator or live supervisor only). Waits up to ten
    seconds for owned Git/SQL cleanup. Unproved cleanup retains the claim and branch ownership; active
    workers are refused.

    Args:
        body (TaskClaimAbortRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskClaimAbortResponse | TaskClaimAbortResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TaskClaimAbortRequest,
) -> Response[TaskClaimAbortResponse | TaskClaimAbortResponse422]:
    """Abort one exact hung pool claim preparation (operator or live supervisor only). Waits up to ten
    seconds for owned Git/SQL cleanup. Unproved cleanup retains the claim and branch ownership; active
    workers are refused.

     Abort one exact hung pool claim preparation (operator or live supervisor only). Waits up to ten
    seconds for owned Git/SQL cleanup. Unproved cleanup retains the claim and branch ownership; active
    workers are refused.

    Args:
        body (TaskClaimAbortRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TaskClaimAbortResponse | TaskClaimAbortResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: TaskClaimAbortRequest,
) -> TaskClaimAbortResponse | TaskClaimAbortResponse422 | None:
    """Abort one exact hung pool claim preparation (operator or live supervisor only). Waits up to ten
    seconds for owned Git/SQL cleanup. Unproved cleanup retains the claim and branch ownership; active
    workers are refused.

     Abort one exact hung pool claim preparation (operator or live supervisor only). Waits up to ten
    seconds for owned Git/SQL cleanup. Unproved cleanup retains the claim and branch ownership; active
    workers are refused.

    Args:
        body (TaskClaimAbortRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TaskClaimAbortResponse | TaskClaimAbortResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
