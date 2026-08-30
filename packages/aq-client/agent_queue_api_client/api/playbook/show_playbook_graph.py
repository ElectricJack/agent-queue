from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.show_playbook_graph_request import ShowPlaybookGraphRequest
from ...models.show_playbook_graph_response import ShowPlaybookGraphResponse
from ...models.show_playbook_graph_response_422 import ShowPlaybookGraphResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ShowPlaybookGraphRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/show-graph",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ShowPlaybookGraphResponse | ShowPlaybookGraphResponse422 | None:
    if response.status_code == 200:
        response_200 = ShowPlaybookGraphResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ShowPlaybookGraphResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ShowPlaybookGraphResponse | ShowPlaybookGraphResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ShowPlaybookGraphRequest,
) -> Response[ShowPlaybookGraphResponse | ShowPlaybookGraphResponse422]:
    """Render a compiled playbook graph as an ASCII diagram or Mermaid flowchart syntax. Shows nodes (with
    type badges), edges, and transition conditions. Useful for understanding playbook structure and
    sharing visual documentation.

     Render a compiled playbook graph as an ASCII diagram or Mermaid flowchart syntax. Shows nodes (with
    type badges), edges, and transition conditions. Useful for understanding playbook structure and
    sharing visual documentation.

    Args:
        body (ShowPlaybookGraphRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ShowPlaybookGraphResponse | ShowPlaybookGraphResponse422]
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
    body: ShowPlaybookGraphRequest,
) -> ShowPlaybookGraphResponse | ShowPlaybookGraphResponse422 | None:
    """Render a compiled playbook graph as an ASCII diagram or Mermaid flowchart syntax. Shows nodes (with
    type badges), edges, and transition conditions. Useful for understanding playbook structure and
    sharing visual documentation.

     Render a compiled playbook graph as an ASCII diagram or Mermaid flowchart syntax. Shows nodes (with
    type badges), edges, and transition conditions. Useful for understanding playbook structure and
    sharing visual documentation.

    Args:
        body (ShowPlaybookGraphRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ShowPlaybookGraphResponse | ShowPlaybookGraphResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ShowPlaybookGraphRequest,
) -> Response[ShowPlaybookGraphResponse | ShowPlaybookGraphResponse422]:
    """Render a compiled playbook graph as an ASCII diagram or Mermaid flowchart syntax. Shows nodes (with
    type badges), edges, and transition conditions. Useful for understanding playbook structure and
    sharing visual documentation.

     Render a compiled playbook graph as an ASCII diagram or Mermaid flowchart syntax. Shows nodes (with
    type badges), edges, and transition conditions. Useful for understanding playbook structure and
    sharing visual documentation.

    Args:
        body (ShowPlaybookGraphRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ShowPlaybookGraphResponse | ShowPlaybookGraphResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ShowPlaybookGraphRequest,
) -> ShowPlaybookGraphResponse | ShowPlaybookGraphResponse422 | None:
    """Render a compiled playbook graph as an ASCII diagram or Mermaid flowchart syntax. Shows nodes (with
    type badges), edges, and transition conditions. Useful for understanding playbook structure and
    sharing visual documentation.

     Render a compiled playbook graph as an ASCII diagram or Mermaid flowchart syntax. Shows nodes (with
    type badges), edges, and transition conditions. Useful for understanding playbook structure and
    sharing visual documentation.

    Args:
        body (ShowPlaybookGraphRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ShowPlaybookGraphResponse | ShowPlaybookGraphResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
