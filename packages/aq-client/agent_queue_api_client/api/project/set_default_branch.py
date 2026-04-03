from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.set_default_branch_request import SetDefaultBranchRequest
from ...models.set_default_branch_response import SetDefaultBranchResponse
from ...models.set_default_branch_response_422 import SetDefaultBranchResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: SetDefaultBranchRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/project/set-default-branch",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> SetDefaultBranchResponse | SetDefaultBranchResponse422 | None:
    if response.status_code == 200:
        response_200 = SetDefaultBranchResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = SetDefaultBranchResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[SetDefaultBranchResponse | SetDefaultBranchResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SetDefaultBranchRequest,
) -> Response[SetDefaultBranchResponse | SetDefaultBranchResponse422]:
    """Set (or change) the default git branch for a project. If the branch does not exist on the remote
    yet, it will be created automatically from the current default branch. Use this when a project
    should branch off of and merge into a branch other than 'main' (e.g. 'dev').

     Set (or change) the default git branch for a project. If the branch does not exist on the remote
    yet, it will be created automatically from the current default branch. Use this when a project
    should branch off of and merge into a branch other than 'main' (e.g. 'dev').

    Args:
        body (SetDefaultBranchRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[SetDefaultBranchResponse | SetDefaultBranchResponse422]
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
    body: SetDefaultBranchRequest,
) -> SetDefaultBranchResponse | SetDefaultBranchResponse422 | None:
    """Set (or change) the default git branch for a project. If the branch does not exist on the remote
    yet, it will be created automatically from the current default branch. Use this when a project
    should branch off of and merge into a branch other than 'main' (e.g. 'dev').

     Set (or change) the default git branch for a project. If the branch does not exist on the remote
    yet, it will be created automatically from the current default branch. Use this when a project
    should branch off of and merge into a branch other than 'main' (e.g. 'dev').

    Args:
        body (SetDefaultBranchRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        SetDefaultBranchResponse | SetDefaultBranchResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: SetDefaultBranchRequest,
) -> Response[SetDefaultBranchResponse | SetDefaultBranchResponse422]:
    """Set (or change) the default git branch for a project. If the branch does not exist on the remote
    yet, it will be created automatically from the current default branch. Use this when a project
    should branch off of and merge into a branch other than 'main' (e.g. 'dev').

     Set (or change) the default git branch for a project. If the branch does not exist on the remote
    yet, it will be created automatically from the current default branch. Use this when a project
    should branch off of and merge into a branch other than 'main' (e.g. 'dev').

    Args:
        body (SetDefaultBranchRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[SetDefaultBranchResponse | SetDefaultBranchResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: SetDefaultBranchRequest,
) -> SetDefaultBranchResponse | SetDefaultBranchResponse422 | None:
    """Set (or change) the default git branch for a project. If the branch does not exist on the remote
    yet, it will be created automatically from the current default branch. Use this when a project
    should branch off of and merge into a branch other than 'main' (e.g. 'dev').

     Set (or change) the default git branch for a project. If the branch does not exist on the remote
    yet, it will be created automatically from the current default branch. Use this when a project
    should branch off of and merge into a branch other than 'main' (e.g. 'dev').

    Args:
        body (SetDefaultBranchRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        SetDefaultBranchResponse | SetDefaultBranchResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
