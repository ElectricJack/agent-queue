from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_allocation_intent_by_status import ProviderAllocationIntentByStatus


T = TypeVar("T", bound="ProviderAllocationIntent")


@_attrs_define
class ProviderAllocationIntent:
    """READY/ASSIGNED/IN_PROGRESS tasks carrying one explicit provider intent.

    ``count`` and ``by_status`` are fleet-wide; ``task_ids`` holds only the
    ones inside the caller's view.

        Attributes:
            count (int | Unset):  Default: 0.
            by_status (ProviderAllocationIntentByStatus | Unset):
            task_ids (list[str] | Unset):
    """

    count: int | Unset = 0
    by_status: ProviderAllocationIntentByStatus | Unset = UNSET
    task_ids: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        count = self.count

        by_status: dict[str, Any] | Unset = UNSET
        if not isinstance(self.by_status, Unset):
            by_status = self.by_status.to_dict()

        task_ids: list[str] | Unset = UNSET
        if not isinstance(self.task_ids, Unset):
            task_ids = self.task_ids

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if count is not UNSET:
            field_dict["count"] = count
        if by_status is not UNSET:
            field_dict["by_status"] = by_status
        if task_ids is not UNSET:
            field_dict["task_ids"] = task_ids

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_allocation_intent_by_status import ProviderAllocationIntentByStatus

        d = dict(src_dict)
        count = d.pop("count", UNSET)

        _by_status = d.pop("by_status", UNSET)
        by_status: ProviderAllocationIntentByStatus | Unset
        if isinstance(_by_status, Unset):
            by_status = UNSET
        else:
            by_status = ProviderAllocationIntentByStatus.from_dict(_by_status)

        task_ids = cast(list[str], d.pop("task_ids", UNSET))

        provider_allocation_intent = cls(
            count=count,
            by_status=by_status,
            task_ids=task_ids,
        )

        provider_allocation_intent.additional_properties = d
        return provider_allocation_intent

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
