from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.test_selection_recheck_request import TestSelectionRecheckRequest
from ...models.test_selection_recheck_response import TestSelectionRecheckResponse
from ...models.test_selection_recheck_response_422 import TestSelectionRecheckResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: TestSelectionRecheckRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/test_selection/recheck",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> TestSelectionRecheckResponse | TestSelectionRecheckResponse422 | None:
    if response.status_code == 200:
        response_200 = TestSelectionRecheckResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = TestSelectionRecheckResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[TestSelectionRecheckResponse | TestSelectionRecheckResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TestSelectionRecheckRequest,
) -> Response[TestSelectionRecheckResponse | TestSelectionRecheckResponse422]:
    """Check whether a recorded selection snapshot is stale.

     Check whether a recorded selection snapshot is stale.

    Args:
        body (TestSelectionRecheckRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TestSelectionRecheckResponse | TestSelectionRecheckResponse422]
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
    body: TestSelectionRecheckRequest,
) -> TestSelectionRecheckResponse | TestSelectionRecheckResponse422 | None:
    """Check whether a recorded selection snapshot is stale.

     Check whether a recorded selection snapshot is stale.

    Args:
        body (TestSelectionRecheckRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TestSelectionRecheckResponse | TestSelectionRecheckResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: TestSelectionRecheckRequest,
) -> Response[TestSelectionRecheckResponse | TestSelectionRecheckResponse422]:
    """Check whether a recorded selection snapshot is stale.

     Check whether a recorded selection snapshot is stale.

    Args:
        body (TestSelectionRecheckRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[TestSelectionRecheckResponse | TestSelectionRecheckResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: TestSelectionRecheckRequest,
) -> TestSelectionRecheckResponse | TestSelectionRecheckResponse422 | None:
    """Check whether a recorded selection snapshot is stale.

     Check whether a recorded selection snapshot is stale.

    Args:
        body (TestSelectionRecheckRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        TestSelectionRecheckResponse | TestSelectionRecheckResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
