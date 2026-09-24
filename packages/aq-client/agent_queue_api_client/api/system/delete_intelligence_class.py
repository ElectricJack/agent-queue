from http import HTTPStatus
from typing import Any

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.delete_intelligence_class_conflict_response import DeleteIntelligenceClassConflictResponse
from ...models.delete_intelligence_class_request import DeleteIntelligenceClassRequest
from ...models.delete_intelligence_class_response import DeleteIntelligenceClassResponse
from ...models.delete_intelligence_class_response_422 import DeleteIntelligenceClassResponse422
from ...models.edit_intelligence_class_conflict_response import EditIntelligenceClassConflictResponse
from ...types import Response


def _get_kwargs(
    *,
    body: DeleteIntelligenceClassRequest,
) -> dict[str, Any]:
    headers: dict[str, Any] = {}

    _kwargs: dict[str, Any] = {
        "method": "post",
        "url": "/api/system/delete-intelligence-class",
    }

    _kwargs["json"] = body.to_dict()

    headers["Content-Type"] = "application/json"

    _kwargs["headers"] = headers
    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> (
    DeleteIntelligenceClassConflictResponse
    | EditIntelligenceClassConflictResponse
    | DeleteIntelligenceClassResponse
    | DeleteIntelligenceClassResponse422
    | None
):
    if response.status_code == 200:
        response_200 = DeleteIntelligenceClassResponse.from_dict(response.json())

        return response_200

    if response.status_code == 409:

        def _parse_response_409(
            data: object,
        ) -> DeleteIntelligenceClassConflictResponse | EditIntelligenceClassConflictResponse:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                response_409_type_0 = DeleteIntelligenceClassConflictResponse.from_dict(data)

                return response_409_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            response_409_type_1 = EditIntelligenceClassConflictResponse.from_dict(data)

            return response_409_type_1

        response_409 = _parse_response_409(response.json())

        return response_409

    if response.status_code == 422:
        response_422 = DeleteIntelligenceClassResponse422.from_dict(response.json())

        return response_422

    if client.raise_on_unexpected_status:
        raise errors.UnexpectedStatus(response.status_code, response.content)
    else:
        return None


def _build_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> Response[
    DeleteIntelligenceClassConflictResponse
    | EditIntelligenceClassConflictResponse
    | DeleteIntelligenceClassResponse
    | DeleteIntelligenceClassResponse422
]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DeleteIntelligenceClassRequest,
) -> Response[
    DeleteIntelligenceClassConflictResponse
    | EditIntelligenceClassConflictResponse
    | DeleteIntelligenceClassResponse
    | DeleteIntelligenceClassResponse422
]:
    """Retire an unreferenced global intelligence class. Refuses when agents, agent profiles or non-
    terminal tasks still use it. Requires global admin. Pass the revision from list_intelligence_classes
    to reject stale deletes.

     Retire an unreferenced global intelligence class. Refuses when agents, agent profiles or non-
    terminal tasks still use it. Requires global admin. Pass the revision from list_intelligence_classes
    to reject stale deletes.

    Args:
        body (DeleteIntelligenceClassRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DeleteIntelligenceClassConflictResponse | EditIntelligenceClassConflictResponse | DeleteIntelligenceClassResponse | DeleteIntelligenceClassResponse422]
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
    body: DeleteIntelligenceClassRequest,
) -> (
    DeleteIntelligenceClassConflictResponse
    | EditIntelligenceClassConflictResponse
    | DeleteIntelligenceClassResponse
    | DeleteIntelligenceClassResponse422
    | None
):
    """Retire an unreferenced global intelligence class. Refuses when agents, agent profiles or non-
    terminal tasks still use it. Requires global admin. Pass the revision from list_intelligence_classes
    to reject stale deletes.

     Retire an unreferenced global intelligence class. Refuses when agents, agent profiles or non-
    terminal tasks still use it. Requires global admin. Pass the revision from list_intelligence_classes
    to reject stale deletes.

    Args:
        body (DeleteIntelligenceClassRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DeleteIntelligenceClassConflictResponse | EditIntelligenceClassConflictResponse | DeleteIntelligenceClassResponse | DeleteIntelligenceClassResponse422
    """

    return sync_detailed(
        client=client,
        body=body,
    ).parsed


async def asyncio_detailed(
    *,
    client: AuthenticatedClient | Client,
    body: DeleteIntelligenceClassRequest,
) -> Response[
    DeleteIntelligenceClassConflictResponse
    | EditIntelligenceClassConflictResponse
    | DeleteIntelligenceClassResponse
    | DeleteIntelligenceClassResponse422
]:
    """Retire an unreferenced global intelligence class. Refuses when agents, agent profiles or non-
    terminal tasks still use it. Requires global admin. Pass the revision from list_intelligence_classes
    to reject stale deletes.

     Retire an unreferenced global intelligence class. Refuses when agents, agent profiles or non-
    terminal tasks still use it. Requires global admin. Pass the revision from list_intelligence_classes
    to reject stale deletes.

    Args:
        body (DeleteIntelligenceClassRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[DeleteIntelligenceClassConflictResponse | EditIntelligenceClassConflictResponse | DeleteIntelligenceClassResponse | DeleteIntelligenceClassResponse422]
    """

    kwargs = _get_kwargs(
        body=body,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    *,
    client: AuthenticatedClient | Client,
    body: DeleteIntelligenceClassRequest,
) -> (
    DeleteIntelligenceClassConflictResponse
    | EditIntelligenceClassConflictResponse
    | DeleteIntelligenceClassResponse
    | DeleteIntelligenceClassResponse422
    | None
):
    """Retire an unreferenced global intelligence class. Refuses when agents, agent profiles or non-
    terminal tasks still use it. Requires global admin. Pass the revision from list_intelligence_classes
    to reject stale deletes.

     Retire an unreferenced global intelligence class. Refuses when agents, agent profiles or non-
    terminal tasks still use it. Requires global admin. Pass the revision from list_intelligence_classes
    to reject stale deletes.

    Args:
        body (DeleteIntelligenceClassRequest):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        DeleteIntelligenceClassConflictResponse | EditIntelligenceClassConflictResponse | DeleteIntelligenceClassResponse | DeleteIntelligenceClassResponse422
    """

    return (
        await asyncio_detailed(
            client=client,
            body=body,
        )
    ).parsed
