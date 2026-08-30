from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_validate_request import PlaybookValidateRequest
from ...models.playbook_validate_response import PlaybookValidateResponse
from ...models.playbook_validate_response_422 import PlaybookValidateResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookValidateRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/validate",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookValidateResponse | PlaybookValidateResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookValidateResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookValidateResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookValidateResponse | PlaybookValidateResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookValidateRequest,
) -> Response[PlaybookValidateResponse | PlaybookValidateResponse422]:
    """Validate a playbook file inside the vault. A ``.md`` source is checked for YAML frontmatter only and
    comes back with ``requires_compile: true`` — compiling it is a separate, agent-produced step. A
    ``.json`` artifact is fully validated against the compiled-playbook schema. Errors are returned as
    structured ``{node, field, message}`` rows, not prose. Paths outside the vault root are refused.

     Validate a playbook file inside the vault. A ``.md`` source is checked for YAML frontmatter only and
    comes back with ``requires_compile: true`` — compiling it is a separate, agent-produced step. A
    ``.json`` artifact is fully validated against the compiled-playbook schema. Errors are returned as
    structured ``{node, field, message}`` rows, not prose. Paths outside the vault root are refused.

    Args:
        body (PlaybookValidateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookValidateResponse | PlaybookValidateResponse422]
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
    body: PlaybookValidateRequest,
) -> PlaybookValidateResponse | PlaybookValidateResponse422 | None:
    """Validate a playbook file inside the vault. A ``.md`` source is checked for YAML frontmatter only and
    comes back with ``requires_compile: true`` — compiling it is a separate, agent-produced step. A
    ``.json`` artifact is fully validated against the compiled-playbook schema. Errors are returned as
    structured ``{node, field, message}`` rows, not prose. Paths outside the vault root are refused.

     Validate a playbook file inside the vault. A ``.md`` source is checked for YAML frontmatter only and
    comes back with ``requires_compile: true`` — compiling it is a separate, agent-produced step. A
    ``.json`` artifact is fully validated against the compiled-playbook schema. Errors are returned as
    structured ``{node, field, message}`` rows, not prose. Paths outside the vault root are refused.

    Args:
        body (PlaybookValidateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookValidateResponse | PlaybookValidateResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookValidateRequest,
) -> Response[PlaybookValidateResponse | PlaybookValidateResponse422]:
    """Validate a playbook file inside the vault. A ``.md`` source is checked for YAML frontmatter only and
    comes back with ``requires_compile: true`` — compiling it is a separate, agent-produced step. A
    ``.json`` artifact is fully validated against the compiled-playbook schema. Errors are returned as
    structured ``{node, field, message}`` rows, not prose. Paths outside the vault root are refused.

     Validate a playbook file inside the vault. A ``.md`` source is checked for YAML frontmatter only and
    comes back with ``requires_compile: true`` — compiling it is a separate, agent-produced step. A
    ``.json`` artifact is fully validated against the compiled-playbook schema. Errors are returned as
    structured ``{node, field, message}`` rows, not prose. Paths outside the vault root are refused.

    Args:
        body (PlaybookValidateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookValidateResponse | PlaybookValidateResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookValidateRequest,
) -> PlaybookValidateResponse | PlaybookValidateResponse422 | None:
    """Validate a playbook file inside the vault. A ``.md`` source is checked for YAML frontmatter only and
    comes back with ``requires_compile: true`` — compiling it is a separate, agent-produced step. A
    ``.json`` artifact is fully validated against the compiled-playbook schema. Errors are returned as
    structured ``{node, field, message}`` rows, not prose. Paths outside the vault root are refused.

     Validate a playbook file inside the vault. A ``.md`` source is checked for YAML frontmatter only and
    comes back with ``requires_compile: true`` — compiling it is a separate, agent-produced step. A
    ``.json`` artifact is fully validated against the compiled-playbook schema. Errors are returned as
    structured ``{node, field, message}`` rows, not prose. Paths outside the vault root are refused.

    Args:
        body (PlaybookValidateRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookValidateResponse | PlaybookValidateResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
