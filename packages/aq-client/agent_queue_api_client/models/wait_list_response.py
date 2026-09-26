from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.agent_wait_record import AgentWaitRecord


T = TypeVar("T", bound="WaitListResponse")


@_attrs_define
class WaitListResponse:
    """
    Attributes:
        waits (list[AgentWaitRecord]):
        count (int):
        success (bool | Unset):  Default: True.
    """

    waits: list[AgentWaitRecord]
    count: int
    success: bool | Unset = True

    def to_dict(self) -> dict[str, Any]:
        waits = []
        for waits_item_data in self.waits:
            waits_item = waits_item_data.to_dict()
            waits.append(waits_item)

        count = self.count

        success = self.success

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "waits": waits,
                "count": count,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_wait_record import AgentWaitRecord

        d = dict(src_dict)
        waits = []
        _waits = d.pop("waits")
        for waits_item_data in _waits:
            waits_item = AgentWaitRecord.from_dict(waits_item_data)

            waits.append(waits_item)

        count = d.pop("count")

        success = d.pop("success", UNSET)

        wait_list_response = cls(
            waits=waits,
            count=count,
            success=success,
        )

        return wait_list_response
