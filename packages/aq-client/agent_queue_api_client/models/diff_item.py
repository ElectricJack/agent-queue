from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.diff_item_original_scope import DiffItemOriginalScope
from ..models.diff_item_scope import DiffItemScope
from ..models.diff_item_state import DiffItemState
from ..models.diff_item_status import DiffItemStatus
from ..models.diff_item_type import DiffItemType
from ..types import UNSET, Unset

T = TypeVar("T", bound="DiffItem")


@_attrs_define
class DiffItem:
    """
    Attributes:
        id (str):
        name (str):
        type_ (DiffItemType):
        original_scope (DiffItemOriginalScope):
        scope (DiffItemScope):
        status (DiffItemStatus):
        selected (bool):
        requires_scope_choice (bool):
        current_checksum (None | str | Unset):
        diff (str | Unset):  Default: ''.
        destinations (list[str] | Unset):
        state (DiffItemState | Unset):  Default: DiffItemState.COPY.
    """

    id: str
    name: str
    type_: DiffItemType
    original_scope: DiffItemOriginalScope
    scope: DiffItemScope
    status: DiffItemStatus
    selected: bool
    requires_scope_choice: bool
    current_checksum: None | str | Unset = UNSET
    diff: str | Unset = ""
    destinations: list[str] | Unset = UNSET
    state: DiffItemState | Unset = DiffItemState.COPY

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        name = self.name

        type_ = self.type_.value

        original_scope = self.original_scope.value

        scope = self.scope.value

        status = self.status.value

        selected = self.selected

        requires_scope_choice = self.requires_scope_choice

        current_checksum: None | str | Unset
        if isinstance(self.current_checksum, Unset):
            current_checksum = UNSET
        else:
            current_checksum = self.current_checksum

        diff = self.diff

        destinations: list[str] | Unset = UNSET
        if not isinstance(self.destinations, Unset):
            destinations = self.destinations

        state: str | Unset = UNSET
        if not isinstance(self.state, Unset):
            state = self.state.value

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "id": id,
                "name": name,
                "type": type_,
                "original_scope": original_scope,
                "scope": scope,
                "status": status,
                "selected": selected,
                "requires_scope_choice": requires_scope_choice,
            }
        )
        if current_checksum is not UNSET:
            field_dict["current_checksum"] = current_checksum
        if diff is not UNSET:
            field_dict["diff"] = diff
        if destinations is not UNSET:
            field_dict["destinations"] = destinations
        if state is not UNSET:
            field_dict["state"] = state

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        name = d.pop("name")

        type_ = DiffItemType(d.pop("type"))

        original_scope = DiffItemOriginalScope(d.pop("original_scope"))

        scope = DiffItemScope(d.pop("scope"))

        status = DiffItemStatus(d.pop("status"))

        selected = d.pop("selected")

        requires_scope_choice = d.pop("requires_scope_choice")

        def _parse_current_checksum(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_checksum = _parse_current_checksum(d.pop("current_checksum", UNSET))

        diff = d.pop("diff", UNSET)

        destinations = cast(list[str], d.pop("destinations", UNSET))

        _state = d.pop("state", UNSET)
        state: DiffItemState | Unset
        if isinstance(_state, Unset):
            state = UNSET
        else:
            state = DiffItemState(_state)

        diff_item = cls(
            id=id,
            name=name,
            type_=type_,
            original_scope=original_scope,
            scope=scope,
            status=status,
            selected=selected,
            requires_scope_choice=requires_scope_choice,
            current_checksum=current_checksum,
            diff=diff,
            destinations=destinations,
            state=state,
        )

        return diff_item
