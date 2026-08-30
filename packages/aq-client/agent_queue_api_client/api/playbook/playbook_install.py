from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_install_request import PlaybookInstallRequest
from ...models.playbook_install_response import PlaybookInstallResponse
from ...models.playbook_install_response_422 import PlaybookInstallResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookInstallRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/install",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookInstallResponse | PlaybookInstallResponse422 | None:
    if response.status_code == 200:
        response_200 = PlaybookInstallResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookInstallResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookInstallResponse | PlaybookInstallResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookInstallRequest,
) -> Response[PlaybookInstallResponse | PlaybookInstallResponse422]:
    """Install a compiled playbook artifact into the live registry. Re-validates server-side before
    installing (a caller's own validation is not trusted), refuses a markdown source, and refuses an
    artifact whose ``id`` does not match the requested playbook_id. Paths outside the vault root are
    refused.

     Install a compiled playbook artifact into the live registry. Re-validates server-side before
    installing (a caller's own validation is not trusted), refuses a markdown source, and refuses an
    artifact whose ``id`` does not match the requested playbook_id. Paths outside the vault root are
    refused.

    Args:
        body (PlaybookInstallRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookInstallResponse | PlaybookInstallResponse422]
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
    body: PlaybookInstallRequest,
) -> PlaybookInstallResponse | PlaybookInstallResponse422 | None:
    """Install a compiled playbook artifact into the live registry. Re-validates server-side before
    installing (a caller's own validation is not trusted), refuses a markdown source, and refuses an
    artifact whose ``id`` does not match the requested playbook_id. Paths outside the vault root are
    refused.

     Install a compiled playbook artifact into the live registry. Re-validates server-side before
    installing (a caller's own validation is not trusted), refuses a markdown source, and refuses an
    artifact whose ``id`` does not match the requested playbook_id. Paths outside the vault root are
    refused.

    Args:
        body (PlaybookInstallRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookInstallResponse | PlaybookInstallResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookInstallRequest,
) -> Response[PlaybookInstallResponse | PlaybookInstallResponse422]:
    """Install a compiled playbook artifact into the live registry. Re-validates server-side before
    installing (a caller's own validation is not trusted), refuses a markdown source, and refuses an
    artifact whose ``id`` does not match the requested playbook_id. Paths outside the vault root are
    refused.

     Install a compiled playbook artifact into the live registry. Re-validates server-side before
    installing (a caller's own validation is not trusted), refuses a markdown source, and refuses an
    artifact whose ``id`` does not match the requested playbook_id. Paths outside the vault root are
    refused.

    Args:
        body (PlaybookInstallRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookInstallResponse | PlaybookInstallResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookInstallRequest,
) -> PlaybookInstallResponse | PlaybookInstallResponse422 | None:
    """Install a compiled playbook artifact into the live registry. Re-validates server-side before
    installing (a caller's own validation is not trusted), refuses a markdown source, and refuses an
    artifact whose ``id`` does not match the requested playbook_id. Paths outside the vault root are
    refused.

     Install a compiled playbook artifact into the live registry. Re-validates server-side before
    installing (a caller's own validation is not trusted), refuses a markdown source, and refuses an
    artifact whose ``id`` does not match the requested playbook_id. Paths outside the vault root are
    refused.

    Args:
        body (PlaybookInstallRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookInstallResponse | PlaybookInstallResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
