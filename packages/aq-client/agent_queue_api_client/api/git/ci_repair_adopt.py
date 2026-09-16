from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.ci_repair_adopt_request import CiRepairAdoptRequest
from ...models.ci_repair_adopt_response import CiRepairAdoptResponse
from ...models.ci_repair_adopt_response_422 import CiRepairAdoptResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: CiRepairAdoptRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/git/ci-repair-adopt",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> CiRepairAdoptResponse | CiRepairAdoptResponse422 | None:
    if response.status_code == 200:
        response_200 = CiRepairAdoptResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = CiRepairAdoptResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[CiRepairAdoptResponse | CiRepairAdoptResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CiRepairAdoptRequest,
) -> Response[CiRepairAdoptResponse | CiRepairAdoptResponse422]:
    """Make a live task the repair for a red branch: key it ``ci-baseline:<signature>:<n>`` and record the
    failing tests it owns, so ci_baseline_status reuses it instead of filing another repair, including
    after a partial fix shrinks the failing set.  Adopt a repair filed by hand with just project_id and
    task_id: the command reads the branch's CI and adopts its whole failure.  A task already recorded is
    returned unchanged.

     Make a live task the repair for a red branch: key it ``ci-baseline:<signature>:<n>`` and record the
    failing tests it owns, so ci_baseline_status reuses it instead of filing another repair, including
    after a partial fix shrinks the failing set.  Adopt a repair filed by hand with just project_id and
    task_id: the command reads the branch's CI and adopts its whole failure.  A task already recorded is
    returned unchanged.

    Args:
        body (CiRepairAdoptRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CiRepairAdoptResponse | CiRepairAdoptResponse422]
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
    body: CiRepairAdoptRequest,
) -> CiRepairAdoptResponse | CiRepairAdoptResponse422 | None:
    """Make a live task the repair for a red branch: key it ``ci-baseline:<signature>:<n>`` and record the
    failing tests it owns, so ci_baseline_status reuses it instead of filing another repair, including
    after a partial fix shrinks the failing set.  Adopt a repair filed by hand with just project_id and
    task_id: the command reads the branch's CI and adopts its whole failure.  A task already recorded is
    returned unchanged.

     Make a live task the repair for a red branch: key it ``ci-baseline:<signature>:<n>`` and record the
    failing tests it owns, so ci_baseline_status reuses it instead of filing another repair, including
    after a partial fix shrinks the failing set.  Adopt a repair filed by hand with just project_id and
    task_id: the command reads the branch's CI and adopts its whole failure.  A task already recorded is
    returned unchanged.

    Args:
        body (CiRepairAdoptRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CiRepairAdoptResponse | CiRepairAdoptResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CiRepairAdoptRequest,
) -> Response[CiRepairAdoptResponse | CiRepairAdoptResponse422]:
    """Make a live task the repair for a red branch: key it ``ci-baseline:<signature>:<n>`` and record the
    failing tests it owns, so ci_baseline_status reuses it instead of filing another repair, including
    after a partial fix shrinks the failing set.  Adopt a repair filed by hand with just project_id and
    task_id: the command reads the branch's CI and adopts its whole failure.  A task already recorded is
    returned unchanged.

     Make a live task the repair for a red branch: key it ``ci-baseline:<signature>:<n>`` and record the
    failing tests it owns, so ci_baseline_status reuses it instead of filing another repair, including
    after a partial fix shrinks the failing set.  Adopt a repair filed by hand with just project_id and
    task_id: the command reads the branch's CI and adopts its whole failure.  A task already recorded is
    returned unchanged.

    Args:
        body (CiRepairAdoptRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CiRepairAdoptResponse | CiRepairAdoptResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: CiRepairAdoptRequest,
) -> CiRepairAdoptResponse | CiRepairAdoptResponse422 | None:
    """Make a live task the repair for a red branch: key it ``ci-baseline:<signature>:<n>`` and record the
    failing tests it owns, so ci_baseline_status reuses it instead of filing another repair, including
    after a partial fix shrinks the failing set.  Adopt a repair filed by hand with just project_id and
    task_id: the command reads the branch's CI and adopts its whole failure.  A task already recorded is
    returned unchanged.

     Make a live task the repair for a red branch: key it ``ci-baseline:<signature>:<n>`` and record the
    failing tests it owns, so ci_baseline_status reuses it instead of filing another repair, including
    after a partial fix shrinks the failing set.  Adopt a repair filed by hand with just project_id and
    task_id: the command reads the branch's CI and adopts its whole failure.  A task already recorded is
    returned unchanged.

    Args:
        body (CiRepairAdoptRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CiRepairAdoptResponse | CiRepairAdoptResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
