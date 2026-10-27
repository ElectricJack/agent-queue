from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="RecoverRetainedHolderRequest")


@_attrs_define
class RecoverRetainedHolderRequest:
    """
    Attributes:
        project_id (str):
        task_id (str):
        session_id (str):
        reason (str):
        workspace_id (None | str | Unset):
        expected_proof (None | str | Unset):
        apply (bool | Unset):  Default: False.
    """

    project_id: str
    task_id: str
    session_id: str
    reason: str
    workspace_id: None | str | Unset = UNSET
    expected_proof: None | str | Unset = UNSET
    apply: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        task_id = self.task_id

        session_id = self.session_id

        reason = self.reason

        workspace_id: None | str | Unset
        if isinstance(self.workspace_id, Unset):
            workspace_id = UNSET
        else:
            workspace_id = self.workspace_id

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
                "task_id": task_id,
                "session_id": session_id,
                "reason": reason,
            }
        )
        if workspace_id is not UNSET:
            field_dict["workspace_id"] = workspace_id
        if expected_proof is not UNSET:
            field_dict["expected_proof"] = expected_proof
        if apply is not UNSET:
            field_dict["apply"] = apply

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        task_id = d.pop("task_id")

        session_id = d.pop("session_id")

        reason = d.pop("reason")

        def _parse_workspace_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        workspace_id = _parse_workspace_id(d.pop("workspace_id", UNSET))

        def _parse_expected_proof(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        expected_proof = _parse_expected_proof(d.pop("expected_proof", UNSET))

        apply = d.pop("apply", UNSET)

        recover_retained_holder_request = cls(
            project_id=project_id,
            task_id=task_id,
            session_id=session_id,
            reason=reason,
            workspace_id=workspace_id,
            expected_proof=expected_proof,
            apply=apply,
        )

        recover_retained_holder_request.additional_properties = d
        return recover_retained_holder_request

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
