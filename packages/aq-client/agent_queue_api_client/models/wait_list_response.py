from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.agent_wait_record import AgentWaitRecord
    from ..models.wait_list_response_waits_item_type_1 import WaitListResponseWaitsItemType1


T = TypeVar("T", bound="WaitListResponse")


@_attrs_define
class WaitListResponse:
    """
    Attributes:
        waits (list[AgentWaitRecord | WaitListResponseWaitsItemType1]):
        count (int):
        success (bool | Unset):  Default: True.
    """

    waits: list[AgentWaitRecord | WaitListResponseWaitsItemType1]
    count: int
    success: bool | Unset = True

    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_wait_record import AgentWaitRecord

        waits = []
        for waits_item_data in self.waits:
            waits_item: dict[str, Any]
            if isinstance(waits_item_data, AgentWaitRecord):
                waits_item = waits_item_data.to_dict()
            else:
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
        from ..models.wait_list_response_waits_item_type_1 import WaitListResponseWaitsItemType1

        d = dict(src_dict)
        waits = []
        _waits = d.pop("waits")
        for waits_item_data in _waits:

            def _parse_waits_item(data: object) -> AgentWaitRecord | WaitListResponseWaitsItemType1:
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    waits_item_type_0 = AgentWaitRecord.from_dict(data)

                    return waits_item_type_0
                except (TypeError, ValueError, AttributeError, KeyError):
                    pass
                if not isinstance(data, dict):
                    raise TypeError()
                waits_item_type_1 = WaitListResponseWaitsItemType1.from_dict(data)

                return waits_item_type_1

            waits_item = _parse_waits_item(waits_item_data)

            waits.append(waits_item)

        count = d.pop("count")

        success = d.pop("success", UNSET)

        wait_list_response = cls(
            waits=waits,
            count=count,
            success=success,
        )

        return wait_list_response
