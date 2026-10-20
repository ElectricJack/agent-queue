from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="RepairWorkspaceCheckoutRequest")


@_attrs_define
class RepairWorkspaceCheckoutRequest:
    """
    Attributes:
        project_id (str):
        workspace_id (str):
        expected_path (str):
        replacement_path (str):
        reason (str):
        expected_proof (None | str | Unset):
        apply (bool | Unset):  Default: False.
    """

    project_id: str
    workspace_id: str
    expected_path: str
    replacement_path: str
    reason: str
    expected_proof: None | str | Unset = UNSET
    apply: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        workspace_id = self.workspace_id

        expected_path = self.expected_path

        replacement_path = self.replacement_path

        reason = self.reason

        expected_proof: None | str | Unset
        if isinstance(self.expected_proof, Unset):
            expected_proof = UNSET
        else:
            expected_proof = self.expected_proof

        apply = self.apply

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "workspace_id": workspace_id,
                "expected_path": expected_path,
                "replacement_path": replacement_path,
                "reason": reason,
            }
        )
        if expected_proof is not UNSET:
            field_dict["expected_proof"] = expected_proof
        if apply is not UNSET:
            field_dict["apply"] = apply

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        workspace_id = d.pop("workspace_id")

        expected_path = d.pop("expected_path")

        replacement_path = d.pop("replacement_path")

        reason = d.pop("reason")

        def _parse_expected_proof(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        expected_proof = _parse_expected_proof(d.pop("expected_proof", UNSET))

        apply = d.pop("apply", UNSET)

        repair_workspace_checkout_request = cls(
            project_id=project_id,
            workspace_id=workspace_id,
            expected_path=expected_path,
            replacement_path=replacement_path,
            reason=reason,
            expected_proof=expected_proof,
            apply=apply,
        )

        repair_workspace_checkout_request.additional_properties = d
        return repair_workspace_checkout_request

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
