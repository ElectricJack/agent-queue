from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.pull_request_approve_response_integration_flush_type_0 import (
        PullRequestApproveResponseIntegrationFlushType0,
    )


T = TypeVar("T", bound="PullRequestApproveResponse")


@_attrs_define
class PullRequestApproveResponse:
    """
    Attributes:
        task_id (str):
        url (str):
        head_sha (str):
        success (bool | Unset):  Default: True.
        review_id (int | None | Unset):
        integration_flush (None | PullRequestApproveResponseIntegrationFlushType0 | Unset):
    """

    task_id: str
    url: str
    head_sha: str
    success: bool | Unset = True
    review_id: int | None | Unset = UNSET
    integration_flush: None | PullRequestApproveResponseIntegrationFlushType0 | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.pull_request_approve_response_integration_flush_type_0 import (
            PullRequestApproveResponseIntegrationFlushType0,
        )

        task_id = self.task_id

        url = self.url

        head_sha = self.head_sha

        success = self.success

        review_id: int | None | Unset
        if isinstance(self.review_id, Unset):
            review_id = UNSET
        else:
            review_id = self.review_id

        integration_flush: dict[str, Any] | None | Unset
        if isinstance(self.integration_flush, Unset):
            integration_flush = UNSET
        elif isinstance(self.integration_flush, PullRequestApproveResponseIntegrationFlushType0):
            integration_flush = self.integration_flush.to_dict()
        else:
            integration_flush = self.integration_flush

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "url": url,
                "head_sha": head_sha,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if review_id is not UNSET:
            field_dict["review_id"] = review_id
        if integration_flush is not UNSET:
            field_dict["integration_flush"] = integration_flush

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.pull_request_approve_response_integration_flush_type_0 import (
            PullRequestApproveResponseIntegrationFlushType0,
        )

        d = dict(src_dict)
        task_id = d.pop("task_id")

        url = d.pop("url")

        head_sha = d.pop("head_sha")

        success = d.pop("success", UNSET)

        def _parse_review_id(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        review_id = _parse_review_id(d.pop("review_id", UNSET))

        def _parse_integration_flush(data: object) -> None | PullRequestApproveResponseIntegrationFlushType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                integration_flush_type_0 = PullRequestApproveResponseIntegrationFlushType0.from_dict(data)

                return integration_flush_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PullRequestApproveResponseIntegrationFlushType0 | Unset, data)

        integration_flush = _parse_integration_flush(d.pop("integration_flush", UNSET))

        pull_request_approve_response = cls(
            task_id=task_id,
            url=url,
            head_sha=head_sha,
            success=success,
            review_id=review_id,
            integration_flush=integration_flush,
        )

        pull_request_approve_response.additional_properties = d
        return pull_request_approve_response

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
