from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.playbook_v1_admission_open_request import PlaybookV1AdmissionOpenRequest
from ...models.playbook_v1_admission_open_response_422 import PlaybookV1AdmissionOpenResponse422
from ...models.playbook_v1_admission_response import PlaybookV1AdmissionResponse
from ...types import Response


def _get_kwargs(
    *,
    body: PlaybookV1AdmissionOpenRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/playbook/v1-admission-open",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> PlaybookV1AdmissionOpenResponse422 | PlaybookV1AdmissionResponse | None:
    if response.status_code == 200:
        response_200 = PlaybookV1AdmissionResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = PlaybookV1AdmissionOpenResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[PlaybookV1AdmissionOpenResponse422 | PlaybookV1AdmissionResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookV1AdmissionOpenRequest,
) -> Response[PlaybookV1AdmissionOpenResponse422 | PlaybookV1AdmissionResponse]:
    """Re-open V1 playbook admission. Operator-only. Refused while the fleet is on the V2 runtime:
    admission open under V2 would let a rollback silently start new V1 runs against unreviewed
    artifacts.

     Re-open V1 playbook admission. Operator-only. Refused while the fleet is on the V2 runtime:
    admission open under V2 would let a rollback silently start new V1 runs against unreviewed
    artifacts.

    Args:
        body (PlaybookV1AdmissionOpenRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookV1AdmissionOpenResponse422 | PlaybookV1AdmissionResponse]
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
    body: PlaybookV1AdmissionOpenRequest,
) -> PlaybookV1AdmissionOpenResponse422 | PlaybookV1AdmissionResponse | None:
    """Re-open V1 playbook admission. Operator-only. Refused while the fleet is on the V2 runtime:
    admission open under V2 would let a rollback silently start new V1 runs against unreviewed
    artifacts.

     Re-open V1 playbook admission. Operator-only. Refused while the fleet is on the V2 runtime:
    admission open under V2 would let a rollback silently start new V1 runs against unreviewed
    artifacts.

    Args:
        body (PlaybookV1AdmissionOpenRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookV1AdmissionOpenResponse422 | PlaybookV1AdmissionResponse
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookV1AdmissionOpenRequest,
) -> Response[PlaybookV1AdmissionOpenResponse422 | PlaybookV1AdmissionResponse]:
    """Re-open V1 playbook admission. Operator-only. Refused while the fleet is on the V2 runtime:
    admission open under V2 would let a rollback silently start new V1 runs against unreviewed
    artifacts.

     Re-open V1 playbook admission. Operator-only. Refused while the fleet is on the V2 runtime:
    admission open under V2 would let a rollback silently start new V1 runs against unreviewed
    artifacts.

    Args:
        body (PlaybookV1AdmissionOpenRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[PlaybookV1AdmissionOpenResponse422 | PlaybookV1AdmissionResponse]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: PlaybookV1AdmissionOpenRequest,
) -> PlaybookV1AdmissionOpenResponse422 | PlaybookV1AdmissionResponse | None:
    """Re-open V1 playbook admission. Operator-only. Refused while the fleet is on the V2 runtime:
    admission open under V2 would let a rollback silently start new V1 runs against unreviewed
    artifacts.

     Re-open V1 playbook admission. Operator-only. Refused while the fleet is on the V2 runtime:
    admission open under V2 would let a rollback silently start new V1 runs against unreviewed
    artifacts.

    Args:
        body (PlaybookV1AdmissionOpenRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        PlaybookV1AdmissionOpenResponse422 | PlaybookV1AdmissionResponse
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
