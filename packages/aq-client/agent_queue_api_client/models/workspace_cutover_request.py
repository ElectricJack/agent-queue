from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WorkspaceCutoverRequest")


@_attrs_define
class WorkspaceCutoverRequest:
    """
    Attributes:
        project_id (str): Project owning both bases.
        source_id (str): Current enabled base.
        target_id (str): New disabled, idle base of the same kind.
        apply (bool | None | Unset): Apply the preview (default false).
        expected_proof (None | str | Unset): Exact SHA256 proof returned by preview.
    """

    project_id: str
    source_id: str
    target_id: str
    apply: bool | None | Unset = UNSET
    expected_proof: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        source_id = self.source_id

        target_id = self.target_id

        apply: bool | None | Unset
        if isinstance(self.apply, Unset):
            apply = UNSET
        else:
            apply = self.apply

        expected_proof: None | str | Unset
        if isinstance(self.expected_proof, Unset):
            expected_proof = UNSET
        else:
            expected_proof = self.expected_proof

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "source_id": source_id,
                "target_id": target_id,
            }
        )
        if apply is not UNSET:
            field_dict["apply"] = apply
        if expected_proof is not UNSET:
            field_dict["expected_proof"] = expected_proof

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        source_id = d.pop("source_id")

        target_id = d.pop("target_id")

        def _parse_apply(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        apply = _parse_apply(d.pop("apply", UNSET))

        def _parse_expected_proof(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        expected_proof = _parse_expected_proof(d.pop("expected_proof", UNSET))

        workspace_cutover_request = cls(
            project_id=project_id,
            source_id=source_id,
            target_id=target_id,
            apply=apply,
            expected_proof=expected_proof,
        )

        workspace_cutover_request.additional_properties = d
        return workspace_cutover_request

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
