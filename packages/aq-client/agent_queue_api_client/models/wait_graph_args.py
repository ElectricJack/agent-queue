from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitGraphArgs")


@_attrs_define
class WaitGraphArgs:
    """
    Attributes:
        project_id (None | str | Unset):
        fleet (bool | Unset):  Default: False.
    """

    project_id: None | str | Unset = UNSET
    fleet: bool | Unset = False

    def to_dict(self) -> dict[str, Any]:
        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        fleet = self.fleet

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if fleet is not UNSET:
            field_dict["fleet"] = fleet

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

        fleet = d.pop("fleet", UNSET)

        wait_graph_args = cls(
            project_id=project_id,
            fleet=fleet,
        )

        return wait_graph_args
