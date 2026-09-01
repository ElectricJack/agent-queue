from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_graph_trigger_filter_type_0 import PlaybookGraphTriggerFilterType0


T = TypeVar("T", bound="PlaybookGraphTrigger")


@_attrs_define
class PlaybookGraphTrigger:
    """One compiled trigger on the visualized playbook.

    Attributes:
        event_type (str):
        filter_ (None | PlaybookGraphTriggerFilterType0 | Unset):
    """

    event_type: str
    filter_: None | PlaybookGraphTriggerFilterType0 | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.playbook_graph_trigger_filter_type_0 import PlaybookGraphTriggerFilterType0  # noqa: PLC0415

        event_type = self.event_type

        filter_: dict[str, Any] | None | Unset
        if isinstance(self.filter_, Unset):
            filter_ = UNSET
        elif isinstance(self.filter_, PlaybookGraphTriggerFilterType0):
            filter_ = self.filter_.to_dict()
        else:
            filter_ = self.filter_

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "event_type": event_type,
            }
        )
        if filter_ is not UNSET:
            field_dict["filter"] = filter_

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_graph_trigger_filter_type_0 import PlaybookGraphTriggerFilterType0  # noqa: PLC0415

        d = dict(src_dict)
        event_type = d.pop("event_type")

        def _parse_filter_(data: object) -> None | PlaybookGraphTriggerFilterType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                filter_type_0 = PlaybookGraphTriggerFilterType0.from_dict(data)

                return filter_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlaybookGraphTriggerFilterType0 | Unset, data)

        filter_ = _parse_filter_(d.pop("filter", UNSET))

        playbook_graph_trigger = cls(
            event_type=event_type,
            filter_=filter_,
        )

        playbook_graph_trigger.additional_properties = d
        return playbook_graph_trigger

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
