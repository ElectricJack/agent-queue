from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="EnsureFidelityReviewRequest")


@_attrs_define
class EnsureFidelityReviewRequest:
    """
    Attributes:
        project_id (str):
        batch_id (str):
        structure_revision (int):
    """

    project_id: str
    batch_id: str
    structure_revision: int
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        batch_id = self.batch_id

        structure_revision = self.structure_revision

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "batch_id": batch_id,
                "structure_revision": structure_revision,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        batch_id = d.pop("batch_id")

        structure_revision = d.pop("structure_revision")

        ensure_fidelity_review_request = cls(
            project_id=project_id,
            batch_id=batch_id,
            structure_revision=structure_revision,
        )

        ensure_fidelity_review_request.additional_properties = d
        return ensure_fidelity_review_request

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
