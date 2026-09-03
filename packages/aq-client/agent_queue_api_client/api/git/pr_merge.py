from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.pr_merge_request import PrMergeRequest
from ...models.pr_merge_response import PrMergeResponse
from ...models.pr_merge_response_422 import PrMergeResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PrMergeRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/git/pr-merge",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PrMergeResponse | PrMergeResponse422 | None:
    if response.status_code == 200:
        response_200 = PrMergeResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PrMergeResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PrMergeResponse | PrMergeResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PrMergeRequest,
) -> Response[PrMergeResponse | PrMergeResponse422]:
    """Merge a GitHub pull request via ``gh pr merge``.  Only callable by profiles that whitelist
    ``pr_merge`` in ``allowed_tools`` (final-reviewer only in the dv2-phase2 configuration).  Returns
    the merged SHA on success (best-effort — callers who need the authoritative SHA should query the
    branch head after this command returns).  The PR's status-check rollup is consulted first according
    to ``integration.merge_ci_policy``; the verdict comes back in the ``ci`` block, and under the
    ``required`` policy a non-green rollup refuses the merge.  The same block's ``base`` says whether
    the head is up to date with its base (``current`` / ``stale`` / ``unknown``): a green head behind
    its base passed against a base that has since moved, and under ``required`` that refuses too until
    the branch is updated and its checks re-run (``integration.merge_require_up_to_date``).

     Merge a GitHub pull request via ``gh pr merge``.  Only callable by profiles that whitelist
    ``pr_merge`` in ``allowed_tools`` (final-reviewer only in the dv2-phase2 configuration).  Returns
    the merged SHA on success (best-effort — callers who need the authoritative SHA should query the
    branch head after this command returns).  The PR's status-check rollup is consulted first according
    to ``integration.merge_ci_policy``; the verdict comes back in the ``ci`` block, and under the
    ``required`` policy a non-green rollup refuses the merge.  The same block's ``base`` says whether
    the head is up to date with its base (``current`` / ``stale`` / ``unknown``): a green head behind
    its base passed against a base that has since moved, and under ``required`` that refuses too until
    the branch is updated and its checks re-run (``integration.merge_require_up_to_date``).

    Args:
        body (PrMergeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PrMergeResponse | PrMergeResponse422]
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
    body: PrMergeRequest,
) -> PrMergeResponse | PrMergeResponse422 | None:
    """Merge a GitHub pull request via ``gh pr merge``.  Only callable by profiles that whitelist
    ``pr_merge`` in ``allowed_tools`` (final-reviewer only in the dv2-phase2 configuration).  Returns
    the merged SHA on success (best-effort — callers who need the authoritative SHA should query the
    branch head after this command returns).  The PR's status-check rollup is consulted first according
    to ``integration.merge_ci_policy``; the verdict comes back in the ``ci`` block, and under the
    ``required`` policy a non-green rollup refuses the merge.  The same block's ``base`` says whether
    the head is up to date with its base (``current`` / ``stale`` / ``unknown``): a green head behind
    its base passed against a base that has since moved, and under ``required`` that refuses too until
    the branch is updated and its checks re-run (``integration.merge_require_up_to_date``).

     Merge a GitHub pull request via ``gh pr merge``.  Only callable by profiles that whitelist
    ``pr_merge`` in ``allowed_tools`` (final-reviewer only in the dv2-phase2 configuration).  Returns
    the merged SHA on success (best-effort — callers who need the authoritative SHA should query the
    branch head after this command returns).  The PR's status-check rollup is consulted first according
    to ``integration.merge_ci_policy``; the verdict comes back in the ``ci`` block, and under the
    ``required`` policy a non-green rollup refuses the merge.  The same block's ``base`` says whether
    the head is up to date with its base (``current`` / ``stale`` / ``unknown``): a green head behind
    its base passed against a base that has since moved, and under ``required`` that refuses too until
    the branch is updated and its checks re-run (``integration.merge_require_up_to_date``).

    Args:
        body (PrMergeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PrMergeResponse | PrMergeResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PrMergeRequest,
) -> Response[PrMergeResponse | PrMergeResponse422]:
    """Merge a GitHub pull request via ``gh pr merge``.  Only callable by profiles that whitelist
    ``pr_merge`` in ``allowed_tools`` (final-reviewer only in the dv2-phase2 configuration).  Returns
    the merged SHA on success (best-effort — callers who need the authoritative SHA should query the
    branch head after this command returns).  The PR's status-check rollup is consulted first according
    to ``integration.merge_ci_policy``; the verdict comes back in the ``ci`` block, and under the
    ``required`` policy a non-green rollup refuses the merge.  The same block's ``base`` says whether
    the head is up to date with its base (``current`` / ``stale`` / ``unknown``): a green head behind
    its base passed against a base that has since moved, and under ``required`` that refuses too until
    the branch is updated and its checks re-run (``integration.merge_require_up_to_date``).

     Merge a GitHub pull request via ``gh pr merge``.  Only callable by profiles that whitelist
    ``pr_merge`` in ``allowed_tools`` (final-reviewer only in the dv2-phase2 configuration).  Returns
    the merged SHA on success (best-effort — callers who need the authoritative SHA should query the
    branch head after this command returns).  The PR's status-check rollup is consulted first according
    to ``integration.merge_ci_policy``; the verdict comes back in the ``ci`` block, and under the
    ``required`` policy a non-green rollup refuses the merge.  The same block's ``base`` says whether
    the head is up to date with its base (``current`` / ``stale`` / ``unknown``): a green head behind
    its base passed against a base that has since moved, and under ``required`` that refuses too until
    the branch is updated and its checks re-run (``integration.merge_require_up_to_date``).

    Args:
        body (PrMergeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PrMergeResponse | PrMergeResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PrMergeRequest,
) -> PrMergeResponse | PrMergeResponse422 | None:
    """Merge a GitHub pull request via ``gh pr merge``.  Only callable by profiles that whitelist
    ``pr_merge`` in ``allowed_tools`` (final-reviewer only in the dv2-phase2 configuration).  Returns
    the merged SHA on success (best-effort — callers who need the authoritative SHA should query the
    branch head after this command returns).  The PR's status-check rollup is consulted first according
    to ``integration.merge_ci_policy``; the verdict comes back in the ``ci`` block, and under the
    ``required`` policy a non-green rollup refuses the merge.  The same block's ``base`` says whether
    the head is up to date with its base (``current`` / ``stale`` / ``unknown``): a green head behind
    its base passed against a base that has since moved, and under ``required`` that refuses too until
    the branch is updated and its checks re-run (``integration.merge_require_up_to_date``).

     Merge a GitHub pull request via ``gh pr merge``.  Only callable by profiles that whitelist
    ``pr_merge`` in ``allowed_tools`` (final-reviewer only in the dv2-phase2 configuration).  Returns
    the merged SHA on success (best-effort — callers who need the authoritative SHA should query the
    branch head after this command returns).  The PR's status-check rollup is consulted first according
    to ``integration.merge_ci_policy``; the verdict comes back in the ``ci`` block, and under the
    ``required`` policy a non-green rollup refuses the merge.  The same block's ``base`` says whether
    the head is up to date with its base (``current`` / ``stale`` / ``unknown``): a green head behind
    its base passed against a base that has since moved, and under ``required`` that refuses too until
    the branch is updated and its checks re-run (``integration.merge_require_up_to_date``).

    Args:
        body (PrMergeRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PrMergeResponse | PrMergeResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
