from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.check_profile_request import CheckProfileRequest
from ...models.check_profile_response import CheckProfileResponse
from ...models.check_profile_response_422 import CheckProfileResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: CheckProfileRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/agent/check-profile",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> CheckProfileResponse | CheckProfileResponse422 | None:
    if response.status_code == 200:
        response_200 = CheckProfileResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = CheckProfileResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[CheckProfileResponse | CheckProfileResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CheckProfileRequest,
) -> Response[CheckProfileResponse | CheckProfileResponse422]:
    """Validate an agent profile's install dependencies. Checks that required commands, npm packages, and
    pip packages are available.

     Validate an agent profile's install dependencies. Checks that required commands, npm packages, and
    pip packages are available.

    Args:
        body (CheckProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CheckProfileResponse | CheckProfileResponse422]
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
    body: CheckProfileRequest,
) -> CheckProfileResponse | CheckProfileResponse422 | None:
    """Validate an agent profile's install dependencies. Checks that required commands, npm packages, and
    pip packages are available.

     Validate an agent profile's install dependencies. Checks that required commands, npm packages, and
    pip packages are available.

    Args:
        body (CheckProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CheckProfileResponse | CheckProfileResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: CheckProfileRequest,
) -> Response[CheckProfileResponse | CheckProfileResponse422]:
    """Validate an agent profile's install dependencies. Checks that required commands, npm packages, and
    pip packages are available.

     Validate an agent profile's install dependencies. Checks that required commands, npm packages, and
    pip packages are available.

    Args:
        body (CheckProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[CheckProfileResponse | CheckProfileResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: CheckProfileRequest,
) -> CheckProfileResponse | CheckProfileResponse422 | None:
    """Validate an agent profile's install dependencies. Checks that required commands, npm packages, and
    pip packages are available.

     Validate an agent profile's install dependencies. Checks that required commands, npm packages, and
    pip packages are available.

    Args:
        body (CheckProfileRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        CheckProfileResponse | CheckProfileResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
