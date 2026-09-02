from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.explanation_row_dto_source import ExplanationRowDTOSource
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.explanation_value_dto import ExplanationValueDTO


T = TypeVar("T", bound="ExplanationRowDTO")


@_attrs_define
class ExplanationRowDTO:
    """A labelled input/output row: ``Project -> this event's project``.

    Attributes:
        label (str):
        value (ExplanationValueDTO): One typed value, in both its human and canonical forms.

            ``display`` is always present and always safe to render.  ``canonical`` is
            the Advanced-view payload and is ``None`` whenever ``redacted`` is true.
        source (ExplanationRowDTOSource):
        required (bool | Unset):  Default: True.
        description (None | str | Unset):
    """

    label: str
    value: ExplanationValueDTO
    source: ExplanationRowDTOSource
    required: bool | Unset = True
    description: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        label = self.label

        value = self.value.to_dict()

        source = self.source.value

        required = self.required

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "label": label,
                "value": value,
                "source": source,
            }
        )
        if required is not UNSET:
            field_dict["required"] = required
        if description is not UNSET:
            field_dict["description"] = description

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.explanation_value_dto import ExplanationValueDTO

        d = dict(src_dict)
        label = d.pop("label")

        value = ExplanationValueDTO.from_dict(d.pop("value"))

        source = ExplanationRowDTOSource(d.pop("source"))

        required = d.pop("required", UNSET)

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        explanation_row_dto = cls(
            label=label,
            value=value,
            source=source,
            required=required,
            description=description,
        )

        return explanation_row_dto
