from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.explanation_value_dto_kind import ExplanationValueDTOKind
from ..types import UNSET, Unset

T = TypeVar("T", bound="ExplanationValueDTO")


@_attrs_define
class ExplanationValueDTO:
    """One typed value, in both its human and canonical forms.

    ``display`` is always present and always safe to render.  ``canonical`` is
    the Advanced-view payload and is ``None`` whenever ``redacted`` is true.

        Attributes:
            kind (ExplanationValueDTOKind):
            display (str):
            canonical (Any | None | Unset):
            redacted (bool | Unset):  Default: False.
            type_name (None | str | Unset):
    """

    kind: ExplanationValueDTOKind
    display: str
    canonical: Any | None | Unset = UNSET
    redacted: bool | Unset = False
    type_name: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind.value

        display = self.display

        canonical: Any | None | Unset
        if isinstance(self.canonical, Unset):
            canonical = UNSET
        else:
            canonical = self.canonical

        redacted = self.redacted

        type_name: None | str | Unset
        if isinstance(self.type_name, Unset):
            type_name = UNSET
        else:
            type_name = self.type_name

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "kind": kind,
                "display": display,
            }
        )
        if canonical is not UNSET:
            field_dict["canonical"] = canonical
        if redacted is not UNSET:
            field_dict["redacted"] = redacted
        if type_name is not UNSET:
            field_dict["type_name"] = type_name

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = ExplanationValueDTOKind(d.pop("kind"))

        display = d.pop("display")

        def _parse_canonical(data: object) -> Any | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Any | None | Unset, data)

        canonical = _parse_canonical(d.pop("canonical", UNSET))

        redacted = d.pop("redacted", UNSET)

        def _parse_type_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        type_name = _parse_type_name(d.pop("type_name", UNSET))

        explanation_value_dto = cls(
            kind=kind,
            display=display,
            canonical=canonical,
            redacted=redacted,
            type_name=type_name,
        )

        return explanation_value_dto
