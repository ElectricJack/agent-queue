from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.provider_reroute_undo_request import ProviderRerouteUndoRequest
from ...models.provider_reroute_undo_response import ProviderRerouteUndoResponse
from ...models.provider_reroute_undo_response_422 import ProviderRerouteUndoResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ProviderRerouteUndoRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/provider/reroute-undo",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ProviderRerouteUndoResponse | ProviderRerouteUndoResponse422 | None:
    if response.status_code == 200:
        response_200 = ProviderRerouteUndoResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ProviderRerouteUndoResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ProviderRerouteUndoResponse | ProviderRerouteUndoResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderRerouteUndoRequest,
) -> Response[ProviderRerouteUndoResponse | ProviderRerouteUndoResponse422]:
    """Undo provider re-routes: return tasks to the profile they were on before (rerouted_from), by
    --batch-id or --task-id.  Refused for a running or claimed task, and while the original provider is
    still unavailable unless --force.  Operators and supervisors only.

     Undo provider re-routes: return tasks to the profile they were on before (rerouted_from), by
    --batch-id or --task-id.  Refused for a running or claimed task, and while the original provider is
    still unavailable unless --force.  Operators and supervisors only.

    Args:
        body (ProviderRerouteUndoRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProviderRerouteUndoResponse | ProviderRerouteUndoResponse422]
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
    body: ProviderRerouteUndoRequest,
) -> ProviderRerouteUndoResponse | ProviderRerouteUndoResponse422 | None:
    """Undo provider re-routes: return tasks to the profile they were on before (rerouted_from), by
    --batch-id or --task-id.  Refused for a running or claimed task, and while the original provider is
    still unavailable unless --force.  Operators and supervisors only.

     Undo provider re-routes: return tasks to the profile they were on before (rerouted_from), by
    --batch-id or --task-id.  Refused for a running or claimed task, and while the original provider is
    still unavailable unless --force.  Operators and supervisors only.

    Args:
        body (ProviderRerouteUndoRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProviderRerouteUndoResponse | ProviderRerouteUndoResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderRerouteUndoRequest,
) -> Response[ProviderRerouteUndoResponse | ProviderRerouteUndoResponse422]:
    """Undo provider re-routes: return tasks to the profile they were on before (rerouted_from), by
    --batch-id or --task-id.  Refused for a running or claimed task, and while the original provider is
    still unavailable unless --force.  Operators and supervisors only.

     Undo provider re-routes: return tasks to the profile they were on before (rerouted_from), by
    --batch-id or --task-id.  Refused for a running or claimed task, and while the original provider is
    still unavailable unless --force.  Operators and supervisors only.

    Args:
        body (ProviderRerouteUndoRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProviderRerouteUndoResponse | ProviderRerouteUndoResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ProviderRerouteUndoRequest,
) -> ProviderRerouteUndoResponse | ProviderRerouteUndoResponse422 | None:
    """Undo provider re-routes: return tasks to the profile they were on before (rerouted_from), by
    --batch-id or --task-id.  Refused for a running or claimed task, and while the original provider is
    still unavailable unless --force.  Operators and supervisors only.

     Undo provider re-routes: return tasks to the profile they were on before (rerouted_from), by
    --batch-id or --task-id.  Refused for a running or claimed task, and while the original provider is
    still unavailable unless --force.  Operators and supervisors only.

    Args:
        body (ProviderRerouteUndoRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProviderRerouteUndoResponse | ProviderRerouteUndoResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
