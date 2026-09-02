from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="LayoutWorker")


@_attrs_define
class LayoutWorker:
    """
    Attributes:
        agent_id (str):
        name (str):
        docked_at (str):
        in_collapsed (bool):
    """

    agent_id: str
    name: str
    docked_at: str
    in_collapsed: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        agent_id = self.agent_id

        name = self.name

        docked_at = self.docked_at

        in_collapsed = self.in_collapsed

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "agent_id": agent_id,
                "name": name,
                "docked_at": docked_at,
                "in_collapsed": in_collapsed,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        agent_id = d.pop("agent_id")

        name = d.pop("name")

        docked_at = d.pop("docked_at")

        in_collapsed = d.pop("in_collapsed")

        layout_worker = cls(
            agent_id=agent_id,
            name=name,
            docked_at=docked_at,
            in_collapsed=in_collapsed,
        )

        layout_worker.additional_properties = d
        return layout_worker

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
