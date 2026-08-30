from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.set_playbook_enabled_request import SetPlaybookEnabledRequest
from ...models.set_playbook_enabled_response import SetPlaybookEnabledResponse
from ...models.set_playbook_enabled_response_422 import SetPlaybookEnabledResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: SetPlaybookEnabledRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/set-enabled",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> SetPlaybookEnabledResponse | SetPlaybookEnabledResponse422 | None:
    if response.status_code == 200:
        response_200 = SetPlaybookEnabledResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = SetPlaybookEnabledResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[SetPlaybookEnabledResponse | SetPlaybookEnabledResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SetPlaybookEnabledRequest,
) -> Response[SetPlaybookEnabledResponse | SetPlaybookEnabledResponse422]:
    """Toggle a playbook's `enabled` frontmatter flag. When set to false, trigger events stop spawning new
    runs and run_playbook refuses unless force=true. In-flight runs are not cancelled — disabling means
    stop new starts, not preempt existing instances.

     Toggle a playbook's `enabled` frontmatter flag. When set to false, trigger events stop spawning new
    runs and run_playbook refuses unless force=true. In-flight runs are not cancelled — disabling means
    stop new starts, not preempt existing instances.

    Args:
        body (SetPlaybookEnabledRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[SetPlaybookEnabledResponse | SetPlaybookEnabledResponse422]
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
    body: SetPlaybookEnabledRequest,
) -> SetPlaybookEnabledResponse | SetPlaybookEnabledResponse422 | None:
    """Toggle a playbook's `enabled` frontmatter flag. When set to false, trigger events stop spawning new
    runs and run_playbook refuses unless force=true. In-flight runs are not cancelled — disabling means
    stop new starts, not preempt existing instances.

     Toggle a playbook's `enabled` frontmatter flag. When set to false, trigger events stop spawning new
    runs and run_playbook refuses unless force=true. In-flight runs are not cancelled — disabling means
    stop new starts, not preempt existing instances.

    Args:
        body (SetPlaybookEnabledRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        SetPlaybookEnabledResponse | SetPlaybookEnabledResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SetPlaybookEnabledRequest,
) -> Response[SetPlaybookEnabledResponse | SetPlaybookEnabledResponse422]:
    """Toggle a playbook's `enabled` frontmatter flag. When set to false, trigger events stop spawning new
    runs and run_playbook refuses unless force=true. In-flight runs are not cancelled — disabling means
    stop new starts, not preempt existing instances.

     Toggle a playbook's `enabled` frontmatter flag. When set to false, trigger events stop spawning new
    runs and run_playbook refuses unless force=true. In-flight runs are not cancelled — disabling means
    stop new starts, not preempt existing instances.

    Args:
        body (SetPlaybookEnabledRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[SetPlaybookEnabledResponse | SetPlaybookEnabledResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: SetPlaybookEnabledRequest,
) -> SetPlaybookEnabledResponse | SetPlaybookEnabledResponse422 | None:
    """Toggle a playbook's `enabled` frontmatter flag. When set to false, trigger events stop spawning new
    runs and run_playbook refuses unless force=true. In-flight runs are not cancelled — disabling means
    stop new starts, not preempt existing instances.

     Toggle a playbook's `enabled` frontmatter flag. When set to false, trigger events stop spawning new
    runs and run_playbook refuses unless force=true. In-flight runs are not cancelled — disabling means
    stop new starts, not preempt existing instances.

    Args:
        body (SetPlaybookEnabledRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        SetPlaybookEnabledResponse | SetPlaybookEnabledResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
