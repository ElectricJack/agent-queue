from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitLegacyBridgeArgs")


@_attrs_define
class WaitLegacyBridgeArgs:
    """
    Attributes:
        project_id (None | str | Unset):
        apply (bool | Unset):  Default: False.
    """

    project_id: None | str | Unset = UNSET
    apply: bool | Unset = False

    def to_dict(self) -> dict[str, Any]:
        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        apply = self.apply

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if apply is not UNSET:
            field_dict["apply"] = apply

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        apply = d.pop("apply", UNSET)

        wait_legacy_bridge_args = cls(
            project_id=project_id,
            apply=apply,
        )

        return wait_legacy_bridge_args
