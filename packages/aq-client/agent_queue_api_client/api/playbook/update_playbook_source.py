from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.update_playbook_source_request import UpdatePlaybookSourceRequest
from ...models.update_playbook_source_response import UpdatePlaybookSourceResponse
from ...models.update_playbook_source_response_422 import UpdatePlaybookSourceResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: UpdatePlaybookSourceRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/update-source",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> UpdatePlaybookSourceResponse | UpdatePlaybookSourceResponse422 | None:
    if response.status_code == 200:
        response_200 = UpdatePlaybookSourceResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = UpdatePlaybookSourceResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[UpdatePlaybookSourceResponse | UpdatePlaybookSourceResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: UpdatePlaybookSourceRequest,
) -> Response[UpdatePlaybookSourceResponse | UpdatePlaybookSourceResponse422]:
    """Write new playbook markdown to the vault atomically and compile synchronously. On successful compile
    returns the new version; on validation failure returns 'errors' with previous compiled version still
    live. If 'expected_source_hash' is supplied and does not match the current vault copy, returns a
    conflict error (vault changed underneath).

     Write new playbook markdown to the vault atomically and compile synchronously. On successful compile
    returns the new version; on validation failure returns 'errors' with previous compiled version still
    live. If 'expected_source_hash' is supplied and does not match the current vault copy, returns a
    conflict error (vault changed underneath).

    Args:
        body (UpdatePlaybookSourceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[UpdatePlaybookSourceResponse | UpdatePlaybookSourceResponse422]
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
    body: UpdatePlaybookSourceRequest,
) -> UpdatePlaybookSourceResponse | UpdatePlaybookSourceResponse422 | None:
    """Write new playbook markdown to the vault atomically and compile synchronously. On successful compile
    returns the new version; on validation failure returns 'errors' with previous compiled version still
    live. If 'expected_source_hash' is supplied and does not match the current vault copy, returns a
    conflict error (vault changed underneath).

     Write new playbook markdown to the vault atomically and compile synchronously. On successful compile
    returns the new version; on validation failure returns 'errors' with previous compiled version still
    live. If 'expected_source_hash' is supplied and does not match the current vault copy, returns a
    conflict error (vault changed underneath).

    Args:
        body (UpdatePlaybookSourceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        UpdatePlaybookSourceResponse | UpdatePlaybookSourceResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: UpdatePlaybookSourceRequest,
) -> Response[UpdatePlaybookSourceResponse | UpdatePlaybookSourceResponse422]:
    """Write new playbook markdown to the vault atomically and compile synchronously. On successful compile
    returns the new version; on validation failure returns 'errors' with previous compiled version still
    live. If 'expected_source_hash' is supplied and does not match the current vault copy, returns a
    conflict error (vault changed underneath).

     Write new playbook markdown to the vault atomically and compile synchronously. On successful compile
    returns the new version; on validation failure returns 'errors' with previous compiled version still
    live. If 'expected_source_hash' is supplied and does not match the current vault copy, returns a
    conflict error (vault changed underneath).

    Args:
        body (UpdatePlaybookSourceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[UpdatePlaybookSourceResponse | UpdatePlaybookSourceResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: UpdatePlaybookSourceRequest,
) -> UpdatePlaybookSourceResponse | UpdatePlaybookSourceResponse422 | None:
    """Write new playbook markdown to the vault atomically and compile synchronously. On successful compile
    returns the new version; on validation failure returns 'errors' with previous compiled version still
    live. If 'expected_source_hash' is supplied and does not match the current vault copy, returns a
    conflict error (vault changed underneath).

     Write new playbook markdown to the vault atomically and compile synchronously. On successful compile
    returns the new version; on validation failure returns 'errors' with previous compiled version still
    live. If 'expected_source_hash' is supplied and does not match the current vault copy, returns a
    conflict error (vault changed underneath).

    Args:
        body (UpdatePlaybookSourceRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        UpdatePlaybookSourceResponse | UpdatePlaybookSourceResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
