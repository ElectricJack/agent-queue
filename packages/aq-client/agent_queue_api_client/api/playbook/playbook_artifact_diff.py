from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_artifact_diff_request import PlaybookArtifactDiffRequest
from ...models.playbook_artifact_diff_response import PlaybookArtifactDiffResponse
from ...models.playbook_artifact_diff_response_422 import PlaybookArtifactDiffResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookArtifactDiffRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/artifact-diff",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookArtifactDiffResponse | PlaybookArtifactDiffResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookArtifactDiffResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookArtifactDiffResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookArtifactDiffResponse | PlaybookArtifactDiffResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookArtifactDiffRequest,
) -> Response[PlaybookArtifactDiffResponse | PlaybookArtifactDiffResponse422]:
    """Semantically diff two Playbook V2 artifacts before activation. Rules match by rule_id, steps by
    (rule_id, step_id) and edges by edge id, so reordering an unordered map reads as unchanged.
    Presentation-only changes (titles, labels, help text) report executable=false and do not block
    activation. Read-only: the diff never activates anything.

     Semantically diff two Playbook V2 artifacts before activation. Rules match by rule_id, steps by
    (rule_id, step_id) and edges by edge id, so reordering an unordered map reads as unchanged.
    Presentation-only changes (titles, labels, help text) report executable=false and do not block
    activation. Read-only: the diff never activates anything.

    Args:
        body (PlaybookArtifactDiffRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookArtifactDiffResponse | PlaybookArtifactDiffResponse422]
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
    body: PlaybookArtifactDiffRequest,
) -> PlaybookArtifactDiffResponse | PlaybookArtifactDiffResponse422 | None:
    """Semantically diff two Playbook V2 artifacts before activation. Rules match by rule_id, steps by
    (rule_id, step_id) and edges by edge id, so reordering an unordered map reads as unchanged.
    Presentation-only changes (titles, labels, help text) report executable=false and do not block
    activation. Read-only: the diff never activates anything.

     Semantically diff two Playbook V2 artifacts before activation. Rules match by rule_id, steps by
    (rule_id, step_id) and edges by edge id, so reordering an unordered map reads as unchanged.
    Presentation-only changes (titles, labels, help text) report executable=false and do not block
    activation. Read-only: the diff never activates anything.

    Args:
        body (PlaybookArtifactDiffRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookArtifactDiffResponse | PlaybookArtifactDiffResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookArtifactDiffRequest,
) -> Response[PlaybookArtifactDiffResponse | PlaybookArtifactDiffResponse422]:
    """Semantically diff two Playbook V2 artifacts before activation. Rules match by rule_id, steps by
    (rule_id, step_id) and edges by edge id, so reordering an unordered map reads as unchanged.
    Presentation-only changes (titles, labels, help text) report executable=false and do not block
    activation. Read-only: the diff never activates anything.

     Semantically diff two Playbook V2 artifacts before activation. Rules match by rule_id, steps by
    (rule_id, step_id) and edges by edge id, so reordering an unordered map reads as unchanged.
    Presentation-only changes (titles, labels, help text) report executable=false and do not block
    activation. Read-only: the diff never activates anything.

    Args:
        body (PlaybookArtifactDiffRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookArtifactDiffResponse | PlaybookArtifactDiffResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookArtifactDiffRequest,
) -> PlaybookArtifactDiffResponse | PlaybookArtifactDiffResponse422 | None:
    """Semantically diff two Playbook V2 artifacts before activation. Rules match by rule_id, steps by
    (rule_id, step_id) and edges by edge id, so reordering an unordered map reads as unchanged.
    Presentation-only changes (titles, labels, help text) report executable=false and do not block
    activation. Read-only: the diff never activates anything.

     Semantically diff two Playbook V2 artifacts before activation. Rules match by rule_id, steps by
    (rule_id, step_id) and edges by edge id, so reordering an unordered map reads as unchanged.
    Presentation-only changes (titles, labels, help text) report executable=false and do not block
    activation. Read-only: the diff never activates anything.

    Args:
        body (PlaybookArtifactDiffRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookArtifactDiffResponse | PlaybookArtifactDiffResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
