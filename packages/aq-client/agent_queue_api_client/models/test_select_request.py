from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TestSelectRequest")


@_attrs_define
class TestSelectRequest:
    """
    Attributes:
        task_id (None | str | Unset):
        claim_epoch (int | None | Unset):
        mode (str | Unset):  Default: 'shadow'.
        base_ref (None | str | Unset):
        targets (list[Any] | Unset):
        narrowing_flags (list[Any] | Unset):
        jev (bool | Unset):  Default: True.
        marker_policy (str | Unset):  Default: 'default'.
        acceptance_commands (list[Any] | Unset):
        workspace (None | str | Unset):
        project_id (None | str | Unset):
    """

    task_id: None | str | Unset = UNSET
    claim_epoch: int | None | Unset = UNSET
    mode: str | Unset = "shadow"
    base_ref: None | str | Unset = UNSET
    targets: list[Any] | Unset = UNSET
    narrowing_flags: list[Any] | Unset = UNSET
    jev: bool | Unset = True
    marker_policy: str | Unset = "default"
    acceptance_commands: list[Any] | Unset = UNSET
    workspace: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        mode = self.mode

        base_ref: None | str | Unset
        if isinstance(self.base_ref, Unset):
            base_ref = UNSET
        else:
            base_ref = self.base_ref

        targets: list[Any] | Unset = UNSET
        if not isinstance(self.targets, Unset):
            targets = self.targets

        narrowing_flags: list[Any] | Unset = UNSET
        if not isinstance(self.narrowing_flags, Unset):
            narrowing_flags = self.narrowing_flags

        jev = self.jev

        marker_policy = self.marker_policy

        acceptance_commands: list[Any] | Unset = UNSET
        if not isinstance(self.acceptance_commands, Unset):
            acceptance_commands = self.acceptance_commands

        workspace: None | str | Unset
        if isinstance(self.workspace, Unset):
            workspace = UNSET
        else:
            workspace = self.workspace

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch
        if mode is not UNSET:
            field_dict["mode"] = mode
        if base_ref is not UNSET:
            field_dict["base_ref"] = base_ref
        if targets is not UNSET:
            field_dict["targets"] = targets
        if narrowing_flags is not UNSET:
            field_dict["narrowing_flags"] = narrowing_flags
        if jev is not UNSET:
            field_dict["jev"] = jev
        if marker_policy is not UNSET:
            field_dict["marker_policy"] = marker_policy
        if acceptance_commands is not UNSET:
            field_dict["acceptance_commands"] = acceptance_commands
        if workspace is not UNSET:
            field_dict["workspace"] = workspace
        if project_id is not UNSET:
            field_dict["project_id"] = project_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        mode = d.pop("mode", UNSET)

        def _parse_base_ref(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        base_ref = _parse_base_ref(d.pop("base_ref", UNSET))

        targets = cast(list[Any], d.pop("targets", UNSET))

        narrowing_flags = cast(list[Any], d.pop("narrowing_flags", UNSET))

        jev = d.pop("jev", UNSET)

        marker_policy = d.pop("marker_policy", UNSET)

        acceptance_commands = cast(list[Any], d.pop("acceptance_commands", UNSET))

        def _parse_workspace(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        workspace = _parse_workspace(d.pop("workspace", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        test_select_request = cls(
            task_id=task_id,
            claim_epoch=claim_epoch,
            mode=mode,
            base_ref=base_ref,
            targets=targets,
            narrowing_flags=narrowing_flags,
            jev=jev,
            marker_policy=marker_policy,
            acceptance_commands=acceptance_commands,
            workspace=workspace,
            project_id=project_id,
        )

        test_select_request.additional_properties = d
        return test_select_request

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
