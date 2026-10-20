from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitLiftArgs")


@_attrs_define
class WaitLiftArgs:
    """
    Attributes:
        wait_id (str):
        response (str):
        project_id (None | str | Unset):
        expected_generation (int | None | Unset):
    """

    wait_id: str
    response: str
    project_id: None | str | Unset = UNSET
    expected_generation: int | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        wait_id = self.wait_id

        response = self.response

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        expected_generation: int | None | Unset
        if isinstance(self.expected_generation, Unset):
            expected_generation = UNSET
        else:
            expected_generation = self.expected_generation

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "wait_id": wait_id,
                "response": response,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if expected_generation is not UNSET:
            field_dict["expected_generation"] = expected_generation

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        wait_id = d.pop("wait_id")

        response = d.pop("response")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_expected_generation(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        expected_generation = _parse_expected_generation(d.pop("expected_generation", UNSET))

        wait_lift_args = cls(
            wait_id=wait_id,
            response=response,
            project_id=project_id,
            expected_generation=expected_generation,
        )

        return wait_lift_args
