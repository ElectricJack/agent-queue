from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_v2_graph_request import PlaybookV2GraphRequest
from ...models.playbook_v2_graph_response import PlaybookV2GraphResponse
from ...models.playbook_v2_graph_response_422 import PlaybookV2GraphResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookV2GraphRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/v2-graph",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookV2GraphResponse | PlaybookV2GraphResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookV2GraphResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookV2GraphResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookV2GraphResponse | PlaybookV2GraphResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookV2GraphRequest,
) -> Response[PlaybookV2GraphResponse | PlaybookV2GraphResponse422]:
    """Get the Playbook V2 semantic graph for one immutable artifact: rules grouped by triggering event,
    one node per typed step with a contract-derived explanation of what it does, and one edge per
    declared transition with a stable content-derived id. Defaults to the playbook's active artifact;
    pass artifact_sha256 to project an exact one (this is how a run overlay pins its graph).

     Get the Playbook V2 semantic graph for one immutable artifact: rules grouped by triggering event,
    one node per typed step with a contract-derived explanation of what it does, and one edge per
    declared transition with a stable content-derived id. Defaults to the playbook's active artifact;
    pass artifact_sha256 to project an exact one (this is how a run overlay pins its graph).

    Args:
        body (PlaybookV2GraphRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookV2GraphResponse | PlaybookV2GraphResponse422]
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
    body: PlaybookV2GraphRequest,
) -> PlaybookV2GraphResponse | PlaybookV2GraphResponse422 | None:
    """Get the Playbook V2 semantic graph for one immutable artifact: rules grouped by triggering event,
    one node per typed step with a contract-derived explanation of what it does, and one edge per
    declared transition with a stable content-derived id. Defaults to the playbook's active artifact;
    pass artifact_sha256 to project an exact one (this is how a run overlay pins its graph).

     Get the Playbook V2 semantic graph for one immutable artifact: rules grouped by triggering event,
    one node per typed step with a contract-derived explanation of what it does, and one edge per
    declared transition with a stable content-derived id. Defaults to the playbook's active artifact;
    pass artifact_sha256 to project an exact one (this is how a run overlay pins its graph).

    Args:
        body (PlaybookV2GraphRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookV2GraphResponse | PlaybookV2GraphResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookV2GraphRequest,
) -> Response[PlaybookV2GraphResponse | PlaybookV2GraphResponse422]:
    """Get the Playbook V2 semantic graph for one immutable artifact: rules grouped by triggering event,
    one node per typed step with a contract-derived explanation of what it does, and one edge per
    declared transition with a stable content-derived id. Defaults to the playbook's active artifact;
    pass artifact_sha256 to project an exact one (this is how a run overlay pins its graph).

     Get the Playbook V2 semantic graph for one immutable artifact: rules grouped by triggering event,
    one node per typed step with a contract-derived explanation of what it does, and one edge per
    declared transition with a stable content-derived id. Defaults to the playbook's active artifact;
    pass artifact_sha256 to project an exact one (this is how a run overlay pins its graph).

    Args:
        body (PlaybookV2GraphRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookV2GraphResponse | PlaybookV2GraphResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookV2GraphRequest,
) -> PlaybookV2GraphResponse | PlaybookV2GraphResponse422 | None:
    """Get the Playbook V2 semantic graph for one immutable artifact: rules grouped by triggering event,
    one node per typed step with a contract-derived explanation of what it does, and one edge per
    declared transition with a stable content-derived id. Defaults to the playbook's active artifact;
    pass artifact_sha256 to project an exact one (this is how a run overlay pins its graph).

     Get the Playbook V2 semantic graph for one immutable artifact: rules grouped by triggering event,
    one node per typed step with a contract-derived explanation of what it does, and one edge per
    declared transition with a stable content-derived id. Defaults to the playbook's active artifact;
    pass artifact_sha256 to project an exact one (this is how a run overlay pins its graph).

    Args:
        body (PlaybookV2GraphRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookV2GraphResponse | PlaybookV2GraphResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
