from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.rule_diff_dto_change import RuleDiffDTOChange
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.field_change_dto import FieldChangeDTO


T = TypeVar("T", bound="RuleDiffDTO")


@_attrs_define
class RuleDiffDTO:
    """
    Attributes:
        rule_id (str):
        change (RuleDiffDTOChange):
        event_type_before (None | str | Unset):
        event_type_after (None | str | Unset):
        step_ids_added (list[str] | Unset):
        step_ids_removed (list[str] | Unset):
        field_changes (list[FieldChangeDTO] | Unset):
    """

    rule_id: str
    change: RuleDiffDTOChange
    event_type_before: None | str | Unset = UNSET
    event_type_after: None | str | Unset = UNSET
    step_ids_added: list[str] | Unset = UNSET
    step_ids_removed: list[str] | Unset = UNSET
    field_changes: list[FieldChangeDTO] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        rule_id = self.rule_id

        change = self.change.value

        event_type_before: None | str | Unset
        if isinstance(self.event_type_before, Unset):
            event_type_before = UNSET
        else:
            event_type_before = self.event_type_before

        event_type_after: None | str | Unset
        if isinstance(self.event_type_after, Unset):
            event_type_after = UNSET
        else:
            event_type_after = self.event_type_after

        step_ids_added: list[str] | Unset = UNSET
        if not isinstance(self.step_ids_added, Unset):
            step_ids_added = self.step_ids_added

        step_ids_removed: list[str] | Unset = UNSET
        if not isinstance(self.step_ids_removed, Unset):
            step_ids_removed = self.step_ids_removed

        field_changes: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.field_changes, Unset):
            field_changes = []
            for field_changes_item_data in self.field_changes:
                field_changes_item = field_changes_item_data.to_dict()
                field_changes.append(field_changes_item)

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "rule_id": rule_id,
                "change": change,
            }
        )
        if event_type_before is not UNSET:
            field_dict["event_type_before"] = event_type_before
        if event_type_after is not UNSET:
            field_dict["event_type_after"] = event_type_after
        if step_ids_added is not UNSET:
            field_dict["step_ids_added"] = step_ids_added
        if step_ids_removed is not UNSET:
            field_dict["step_ids_removed"] = step_ids_removed
        if field_changes is not UNSET:
            field_dict["field_changes"] = field_changes

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.field_change_dto import FieldChangeDTO

        d = dict(src_dict)
        rule_id = d.pop("rule_id")

        change = RuleDiffDTOChange(d.pop("change"))

        def _parse_event_type_before(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        event_type_before = _parse_event_type_before(d.pop("event_type_before", UNSET))

        def _parse_event_type_after(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        event_type_after = _parse_event_type_after(d.pop("event_type_after", UNSET))

        step_ids_added = cast(list[str], d.pop("step_ids_added", UNSET))

        step_ids_removed = cast(list[str], d.pop("step_ids_removed", UNSET))

        _field_changes = d.pop("field_changes", UNSET)
        field_changes: list[FieldChangeDTO] | Unset = UNSET
        if _field_changes is not UNSET:
            field_changes = []
            for field_changes_item_data in _field_changes:
                field_changes_item = FieldChangeDTO.from_dict(field_changes_item_data)

                field_changes.append(field_changes_item)

        rule_diff_dto = cls(
            rule_id=rule_id,
            change=change,
            event_type_before=event_type_before,
            event_type_after=event_type_after,
            step_ids_added=step_ids_added,
            step_ids_removed=step_ids_removed,
            field_changes=field_changes,
        )

        return rule_diff_dto
