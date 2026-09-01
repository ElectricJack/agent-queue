from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_graph_trigger import PlaybookGraphTrigger


T = TypeVar("T", bound="PlaybookGraphIdentity")


@_attrs_define
class PlaybookGraphIdentity:
    """Identity block of the graph view — the compiled playbook itself.

    Attributes:
        id (str):
        version (int | Unset):  Default: 0.
        scope (str | Unset):  Default: ''.
        triggers (list[PlaybookGraphTrigger] | Unset):
        node_count (int | Unset):  Default: 0.
        compiled_at (None | str | Unset):
    """

    id: str
    version: int | Unset = 0
    scope: str | Unset = ""
    triggers: list[PlaybookGraphTrigger] | Unset = UNSET
    node_count: int | Unset = 0
    compiled_at: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        version = self.version

        scope = self.scope

        triggers: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.triggers, Unset):
            triggers = []
            for triggers_item_data in self.triggers:
                triggers_item = triggers_item_data.to_dict()
                triggers.append(triggers_item)

        node_count = self.node_count

        compiled_at: None | str | Unset
        if isinstance(self.compiled_at, Unset):
            compiled_at = UNSET
        else:
            compiled_at = self.compiled_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
            }
        )
        if version is not UNSET:
            field_dict["version"] = version
        if scope is not UNSET:
            field_dict["scope"] = scope
        if triggers is not UNSET:
            field_dict["triggers"] = triggers
        if node_count is not UNSET:
            field_dict["node_count"] = node_count
        if compiled_at is not UNSET:
            field_dict["compiled_at"] = compiled_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_graph_trigger import PlaybookGraphTrigger  # noqa: PLC0415

        d = dict(src_dict)
        id = d.pop("id")

        version = d.pop("version", UNSET)

        scope = d.pop("scope", UNSET)

        _triggers = d.pop("triggers", UNSET)
        triggers: list[PlaybookGraphTrigger] | Unset = UNSET
        if _triggers is not UNSET:
            triggers = []
            for triggers_item_data in _triggers:
                triggers_item = PlaybookGraphTrigger.from_dict(triggers_item_data)

                triggers.append(triggers_item)

        node_count = d.pop("node_count", UNSET)

        def _parse_compiled_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        compiled_at = _parse_compiled_at(d.pop("compiled_at", UNSET))

        playbook_graph_identity = cls(
            id=id,
            version=version,
            scope=scope,
            triggers=triggers,
            node_count=node_count,
            compiled_at=compiled_at,
        )

        playbook_graph_identity.additional_properties = d
        return playbook_graph_identity

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
