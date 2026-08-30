from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.get_proposal_api_proposals_proposal_id_get_response_get_proposal_api_proposals_proposal_id_get import (
    GetProposalApiProposalsProposalIdGetResponseGetProposalApiProposalsProposalIdGet,
)
from ...models.http_validation_error import HTTPValidationError
from ...types import Response


def _get_kwargs(
    proposal_id: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "get",
        "url": "/api/proposals/{proposal_id}".format(
            proposal_id=quote(str(proposal_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> GetProposalApiProposalsProposalIdGetResponseGetProposalApiProposalsProposalIdGet | HTTPValidationError | None:
    if response.status_code == 200:
        response_200 = GetProposalApiProposalsProposalIdGetResponseGetProposalApiProposalsProposalIdGet.from_dict(
            response.json()
        )

        return response_200

    if response.status_code == 422:
        response_422 = HTTPValidationError.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[GetProposalApiProposalsProposalIdGetResponseGetProposalApiProposalsProposalIdGet | HTTPValidationError]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    proposal_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[GetProposalApiProposalsProposalIdGetResponseGetProposalApiProposalsProposalIdGet | HTTPValidationError]:
    """Get Proposal

    Args:
        proposal_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetProposalApiProposalsProposalIdGetResponseGetProposalApiProposalsProposalIdGet | HTTPValidationError]
    """

    kwargs = _get_kwargs(
        proposal_id=proposal_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    proposal_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> GetProposalApiProposalsProposalIdGetResponseGetProposalApiProposalsProposalIdGet | HTTPValidationError | None:
    """Get Proposal

    Args:
        proposal_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetProposalApiProposalsProposalIdGetResponseGetProposalApiProposalsProposalIdGet | HTTPValidationError
    """

    return sync_detailed(
        proposal_id=proposal_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    proposal_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[GetProposalApiProposalsProposalIdGetResponseGetProposalApiProposalsProposalIdGet | HTTPValidationError]:
    """Get Proposal

    Args:
        proposal_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[GetProposalApiProposalsProposalIdGetResponseGetProposalApiProposalsProposalIdGet | HTTPValidationError]
    """

    kwargs = _get_kwargs(
        proposal_id=proposal_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    proposal_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> GetProposalApiProposalsProposalIdGetResponseGetProposalApiProposalsProposalIdGet | HTTPValidationError | None:
    """Get Proposal

    Args:
        proposal_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        GetProposalApiProposalsProposalIdGetResponseGetProposalApiProposalsProposalIdGet | HTTPValidationError
    """

    return (
        await asyncio_detailed(
            proposal_id=proposal_id,
            client=client,
        )
    ).parsed
