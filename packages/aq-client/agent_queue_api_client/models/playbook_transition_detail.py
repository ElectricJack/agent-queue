from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_transition_detail_when_type_1 import PlaybookTransitionDetailWhenType1


T = TypeVar("T", bound="PlaybookTransitionDetail")


@_attrs_define
class PlaybookTransitionDetail:
    """One compiled transition as serialized by ``PlaybookTransition.to_dict``.

    Attributes:
        goto (str):
        when (None | PlaybookTransitionDetailWhenType1 | str | Unset):
        otherwise (bool | None | Unset):
    """

    goto: str
    when: None | PlaybookTransitionDetailWhenType1 | str | Unset = UNSET
    otherwise: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.playbook_transition_detail_when_type_1 import PlaybookTransitionDetailWhenType1  # noqa: PLC0415

        goto = self.goto

        when: dict[str, Any] | None | str | Unset
        if isinstance(self.when, Unset):
            when = UNSET
        elif isinstance(self.when, PlaybookTransitionDetailWhenType1):
            when = self.when.to_dict()
        else:
            when = self.when

        otherwise: bool | None | Unset
        if isinstance(self.otherwise, Unset):
            otherwise = UNSET
        else:
            otherwise = self.otherwise

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "goto": goto,
            }
        )
        if when is not UNSET:
            field_dict["when"] = when
        if otherwise is not UNSET:
            field_dict["otherwise"] = otherwise

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_transition_detail_when_type_1 import PlaybookTransitionDetailWhenType1  # noqa: PLC0415

        d = dict(src_dict)
        goto = d.pop("goto")

        def _parse_when(data: object) -> None | PlaybookTransitionDetailWhenType1 | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                when_type_1 = PlaybookTransitionDetailWhenType1.from_dict(data)

                return when_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlaybookTransitionDetailWhenType1 | str | Unset, data)

        when = _parse_when(d.pop("when", UNSET))

        def _parse_otherwise(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        otherwise = _parse_otherwise(d.pop("otherwise", UNSET))

        playbook_transition_detail = cls(
            goto=goto,
            when=when,
            otherwise=otherwise,
        )

        playbook_transition_detail.additional_properties = d
        return playbook_transition_detail

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
