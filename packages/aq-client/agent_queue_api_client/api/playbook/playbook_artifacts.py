from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.list_playbook_artifacts_response import ListPlaybookArtifactsResponse
from ...models.playbook_artifacts_request import PlaybookArtifactsRequest
from ...models.playbook_artifacts_response_422 import PlaybookArtifactsResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookArtifactsRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/artifacts",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ListPlaybookArtifactsResponse | PlaybookArtifactsResponse422 | None:
    if response.status_code == 200:
        response_200 = ListPlaybookArtifactsResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookArtifactsResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ListPlaybookArtifactsResponse | PlaybookArtifactsResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookArtifactsRequest,
) -> Response[ListPlaybookArtifactsResponse | PlaybookArtifactsResponse422]:
    """List the stored Playbook V2 artifacts of one playbook, newest version first, with the currently
    active one flagged. This is the read behind the activation chooser: activation health names only the
    artifact a scope already activated, so this is how an inactive candidate is named before it is
    diffed and activated. Read-only; it never loads an artifact's bytes.

     List the stored Playbook V2 artifacts of one playbook, newest version first, with the currently
    active one flagged. This is the read behind the activation chooser: activation health names only the
    artifact a scope already activated, so this is how an inactive candidate is named before it is
    diffed and activated. Read-only; it never loads an artifact's bytes.

    Args:
        body (PlaybookArtifactsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListPlaybookArtifactsResponse | PlaybookArtifactsResponse422]
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
    body: PlaybookArtifactsRequest,
) -> ListPlaybookArtifactsResponse | PlaybookArtifactsResponse422 | None:
    """List the stored Playbook V2 artifacts of one playbook, newest version first, with the currently
    active one flagged. This is the read behind the activation chooser: activation health names only the
    artifact a scope already activated, so this is how an inactive candidate is named before it is
    diffed and activated. Read-only; it never loads an artifact's bytes.

     List the stored Playbook V2 artifacts of one playbook, newest version first, with the currently
    active one flagged. This is the read behind the activation chooser: activation health names only the
    artifact a scope already activated, so this is how an inactive candidate is named before it is
    diffed and activated. Read-only; it never loads an artifact's bytes.

    Args:
        body (PlaybookArtifactsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListPlaybookArtifactsResponse | PlaybookArtifactsResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookArtifactsRequest,
) -> Response[ListPlaybookArtifactsResponse | PlaybookArtifactsResponse422]:
    """List the stored Playbook V2 artifacts of one playbook, newest version first, with the currently
    active one flagged. This is the read behind the activation chooser: activation health names only the
    artifact a scope already activated, so this is how an inactive candidate is named before it is
    diffed and activated. Read-only; it never loads an artifact's bytes.

     List the stored Playbook V2 artifacts of one playbook, newest version first, with the currently
    active one flagged. This is the read behind the activation chooser: activation health names only the
    artifact a scope already activated, so this is how an inactive candidate is named before it is
    diffed and activated. Read-only; it never loads an artifact's bytes.

    Args:
        body (PlaybookArtifactsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ListPlaybookArtifactsResponse | PlaybookArtifactsResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookArtifactsRequest,
) -> ListPlaybookArtifactsResponse | PlaybookArtifactsResponse422 | None:
    """List the stored Playbook V2 artifacts of one playbook, newest version first, with the currently
    active one flagged. This is the read behind the activation chooser: activation health names only the
    artifact a scope already activated, so this is how an inactive candidate is named before it is
    diffed and activated. Read-only; it never loads an artifact's bytes.

     List the stored Playbook V2 artifacts of one playbook, newest version first, with the currently
    active one flagged. This is the read behind the activation chooser: activation health names only the
    artifact a scope already activated, so this is how an inactive candidate is named before it is
    diffed and activated. Read-only; it never loads an artifact's bytes.

    Args:
        body (PlaybookArtifactsRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ListPlaybookArtifactsResponse | PlaybookArtifactsResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
