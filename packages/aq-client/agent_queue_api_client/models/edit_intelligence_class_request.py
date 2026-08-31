from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.edit_intelligence_class_request_mapping import EditIntelligenceClassRequestMapping


T = TypeVar("T", bound="EditIntelligenceClassRequest")


@_attrs_define
class EditIntelligenceClassRequest:
    """
    Attributes:
        class_id (str): Existing immutable class ID.
        name (str): Human-readable class name.
        description (str): Class description.
        mapping (EditIntelligenceClassRequestMapping): Complete provider-to-configuration JSON mapping.
        expected_revision (None | str | Unset): Raw-file revision from the last read.
    """

    class_id: str
    name: str
    description: str
    mapping: EditIntelligenceClassRequestMapping
    expected_revision: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        class_id = self.class_id

        name = self.name

        description = self.description

        mapping = self.mapping.to_dict()

        expected_revision: None | str | Unset
        if isinstance(self.expected_revision, Unset):
            expected_revision = UNSET
        else:
            expected_revision = self.expected_revision

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "class_id": class_id,
                "name": name,
                "description": description,
                "mapping": mapping,
            }
        )
        if expected_revision is not UNSET:
            field_dict["expected_revision"] = expected_revision

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.edit_intelligence_class_request_mapping import (
            EditIntelligenceClassRequestMapping,  # noqa: PLC0415
        )

        d = dict(src_dict)
        class_id = d.pop("class_id")

        name = d.pop("name")

        description = d.pop("description")

        mapping = EditIntelligenceClassRequestMapping.from_dict(d.pop("mapping"))

        def _parse_expected_revision(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        expected_revision = _parse_expected_revision(d.pop("expected_revision", UNSET))

        edit_intelligence_class_request = cls(
            class_id=class_id,
            name=name,
            description=description,
            mapping=mapping,
            expected_revision=expected_revision,
        )

        edit_intelligence_class_request.additional_properties = d
        return edit_intelligence_class_request

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
