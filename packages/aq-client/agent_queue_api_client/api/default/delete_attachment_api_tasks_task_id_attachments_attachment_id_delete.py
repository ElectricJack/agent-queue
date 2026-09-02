from http import HTTPStatus
from typing import Any
from urllib.parse import quote

import httpx

from ... import errors
from ...client import AuthenticatedClient, Client
from ...models.http_validation_error import HTTPValidationError
from ...models.task_attachment_delete_response import TaskAttachmentDeleteResponse
from ...types import Response


def _get_kwargs(
    task_id: str,
    attachment_id: str,
) -> dict[str, Any]:

    _kwargs: dict[str, Any] = {
        "method": "delete",
        "url": "/api/tasks/{task_id}/attachments/{attachment_id}".format(
            task_id=quote(str(task_id), safe=""),
            attachment_id=quote(str(attachment_id), safe=""),
        ),
    }

    return _kwargs


def _parse_response(
    *, client: AuthenticatedClient | Client, response: httpx.Response
) -> HTTPValidationError | TaskAttachmentDeleteResponse | None:
    if response.status_code == 200:
        response_200 = TaskAttachmentDeleteResponse.from_dict(response.json())

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
) -> Response[HTTPValidationError | TaskAttachmentDeleteResponse]:
    return Response(
        status_code=HTTPStatus(response.status_code),
        content=response.content,
        headers=response.headers,
        parsed=_parse_response(client=client, response=response),
    )


def sync_detailed(
    task_id: str,
    attachment_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[HTTPValidationError | TaskAttachmentDeleteResponse]:
    """Delete Attachment

    Args:
        task_id (str):
        attachment_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | TaskAttachmentDeleteResponse]
    """

    kwargs = _get_kwargs(
        task_id=task_id,
        attachment_id=attachment_id,
    )

    response = client.get_httpx_client().request(
        **kwargs,
    )

    return _build_response(client=client, response=response)


def sync(
    task_id: str,
    attachment_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> HTTPValidationError | TaskAttachmentDeleteResponse | None:
    """Delete Attachment

    Args:
        task_id (str):
        attachment_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | TaskAttachmentDeleteResponse
    """

    return sync_detailed(
        task_id=task_id,
        attachment_id=attachment_id,
        client=client,
    ).parsed


async def asyncio_detailed(
    task_id: str,
    attachment_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> Response[HTTPValidationError | TaskAttachmentDeleteResponse]:
    """Delete Attachment

    Args:
        task_id (str):
        attachment_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        Response[HTTPValidationError | TaskAttachmentDeleteResponse]
    """

    kwargs = _get_kwargs(
        task_id=task_id,
        attachment_id=attachment_id,
    )

    response = await client.get_async_httpx_client().request(**kwargs)

    return _build_response(client=client, response=response)


async def asyncio(
    task_id: str,
    attachment_id: str,
    *,
    client: AuthenticatedClient | Client,
) -> HTTPValidationError | TaskAttachmentDeleteResponse | None:
    """Delete Attachment

    Args:
        task_id (str):
        attachment_id (str):

    Raises:
        errors.UnexpectedStatus: If the server returns an undocumented status code and Client.raise_on_unexpected_status is True.
        httpx.TimeoutException: If the request takes longer than Client.timeout.

    Returns:
        HTTPValidationError | TaskAttachmentDeleteResponse
    """

    return (
        await asyncio_detailed(
            task_id=task_id,
            attachment_id=attachment_id,
            client=client,
        )
    ).parsed
