from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.ci_baseline_status_request import CiBaselineStatusRequest
from ...models.ci_baseline_status_response import CiBaselineStatusResponse
from ...models.ci_baseline_status_response_422 import CiBaselineStatusResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: CiBaselineStatusRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/git/ci-baseline-status",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> CiBaselineStatusResponse | CiBaselineStatusResponse422 | None:
    if response.status_code == 200:
        response_200 = CiBaselineStatusResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = CiBaselineStatusResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[CiBaselineStatusResponse | CiBaselineStatusResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CiBaselineStatusRequest,
) -> Response[CiBaselineStatusResponse | CiBaselineStatusResponse422]:
    """Read the CI verdict for a project's default branch head (or ``ref``): green / red / pending /
    unknown, the failing checks and pytest node ids, and a failure signature.  Read-only.  When red it
    also returns the deduplication key, title and description of the repair task the ci-main-sentinel
    playbook files, and ``escalated`` once the same signature has already burned two repair attempts.

     Read the CI verdict for a project's default branch head (or ``ref``): green / red / pending /
    unknown, the failing checks and pytest node ids, and a failure signature.  Read-only.  When red it
    also returns the deduplication key, title and description of the repair task the ci-main-sentinel
    playbook files, and ``escalated`` once the same signature has already burned two repair attempts.

    Args:
        body (CiBaselineStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CiBaselineStatusResponse | CiBaselineStatusResponse422]
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
    body: CiBaselineStatusRequest,
) -> CiBaselineStatusResponse | CiBaselineStatusResponse422 | None:
    """Read the CI verdict for a project's default branch head (or ``ref``): green / red / pending /
    unknown, the failing checks and pytest node ids, and a failure signature.  Read-only.  When red it
    also returns the deduplication key, title and description of the repair task the ci-main-sentinel
    playbook files, and ``escalated`` once the same signature has already burned two repair attempts.

     Read the CI verdict for a project's default branch head (or ``ref``): green / red / pending /
    unknown, the failing checks and pytest node ids, and a failure signature.  Read-only.  When red it
    also returns the deduplication key, title and description of the repair task the ci-main-sentinel
    playbook files, and ``escalated`` once the same signature has already burned two repair attempts.

    Args:
        body (CiBaselineStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CiBaselineStatusResponse | CiBaselineStatusResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CiBaselineStatusRequest,
) -> Response[CiBaselineStatusResponse | CiBaselineStatusResponse422]:
    """Read the CI verdict for a project's default branch head (or ``ref``): green / red / pending /
    unknown, the failing checks and pytest node ids, and a failure signature.  Read-only.  When red it
    also returns the deduplication key, title and description of the repair task the ci-main-sentinel
    playbook files, and ``escalated`` once the same signature has already burned two repair attempts.

     Read the CI verdict for a project's default branch head (or ``ref``): green / red / pending /
    unknown, the failing checks and pytest node ids, and a failure signature.  Read-only.  When red it
    also returns the deduplication key, title and description of the repair task the ci-main-sentinel
    playbook files, and ``escalated`` once the same signature has already burned two repair attempts.

    Args:
        body (CiBaselineStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CiBaselineStatusResponse | CiBaselineStatusResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: CiBaselineStatusRequest,
) -> CiBaselineStatusResponse | CiBaselineStatusResponse422 | None:
    """Read the CI verdict for a project's default branch head (or ``ref``): green / red / pending /
    unknown, the failing checks and pytest node ids, and a failure signature.  Read-only.  When red it
    also returns the deduplication key, title and description of the repair task the ci-main-sentinel
    playbook files, and ``escalated`` once the same signature has already burned two repair attempts.

     Read the CI verdict for a project's default branch head (or ``ref``): green / red / pending /
    unknown, the failing checks and pytest node ids, and a failure signature.  Read-only.  When red it
    also returns the deduplication key, title and description of the repair task the ci-main-sentinel
    playbook files, and ``escalated`` once the same signature has already burned two repair attempts.

    Args:
        body (CiBaselineStatusRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CiBaselineStatusResponse | CiBaselineStatusResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
