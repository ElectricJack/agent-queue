from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.project_doctor_request import ProjectDoctorRequest
from ...models.project_doctor_response import ProjectDoctorResponse
from ...models.project_doctor_response_422 import ProjectDoctorResponse422
from ...types import Response


def _get_kwargs(
    *,
    body: ProjectDoctorRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/project/doctor",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> ProjectDoctorResponse | ProjectDoctorResponse422 | None:
    if response.status_code == 200:
        response_200 = ProjectDoctorResponse.from_dict(response.json())

        return response_200

    if response.status_code == 422:
        response_422 = ProjectDoctorResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[ProjectDoctorResponse | ProjectDoctorResponse422]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProjectDoctorRequest,
) -> Response[ProjectDoctorResponse | ProjectDoctorResponse422]:
    """Check a project's repository authorization and offer an audited binding command.

     Check a project's repository authorization and offer an audited binding command.

    Args:
        body (ProjectDoctorRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProjectDoctorResponse | ProjectDoctorResponse422]
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
    body: ProjectDoctorRequest,
) -> ProjectDoctorResponse | ProjectDoctorResponse422 | None:
    """Check a project's repository authorization and offer an audited binding command.

     Check a project's repository authorization and offer an audited binding command.

    Args:
        body (ProjectDoctorRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProjectDoctorResponse | ProjectDoctorResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: ProjectDoctorRequest,
) -> Response[ProjectDoctorResponse | ProjectDoctorResponse422]:
    """Check a project's repository authorization and offer an audited binding command.

     Check a project's repository authorization and offer an audited binding command.

    Args:
        body (ProjectDoctorRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[ProjectDoctorResponse | ProjectDoctorResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: ProjectDoctorRequest,
) -> ProjectDoctorResponse | ProjectDoctorResponse422 | None:
    """Check a project's repository authorization and offer an audited binding command.

     Check a project's repository authorization and offer an audited binding command.

    Args:
        body (ProjectDoctorRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        ProjectDoctorResponse | ProjectDoctorResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
