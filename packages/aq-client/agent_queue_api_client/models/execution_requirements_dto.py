from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="ExecutionRequirementsDTO")


@_attrs_define
class ExecutionRequirementsDTO:
    """Reviewed requirements, never a grant or a resolved principal.

    Attributes:
        required_features (list[str] | Unset):
        required_capabilities (list[str] | Unset):
        state_schema_version (int | None | Unset):
    """

    required_features: list[str] | Unset = UNSET
    required_capabilities: list[str] | Unset = UNSET
    state_schema_version: int | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        required_features: list[str] | Unset = UNSET
        if not isinstance(self.required_features, Unset):
            required_features = self.required_features

        required_capabilities: list[str] | Unset = UNSET
        if not isinstance(self.required_capabilities, Unset):
            required_capabilities = self.required_capabilities

        state_schema_version: int | None | Unset
        if isinstance(self.state_schema_version, Unset):
            state_schema_version = UNSET
        else:
            state_schema_version = self.state_schema_version

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if required_features is not UNSET:
            field_dict["required_features"] = required_features
        if required_capabilities is not UNSET:
            field_dict["required_capabilities"] = required_capabilities
        if state_schema_version is not UNSET:
            field_dict["state_schema_version"] = state_schema_version

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        required_features = cast(list[str], d.pop("required_features", UNSET))

        required_capabilities = cast(list[str], d.pop("required_capabilities", UNSET))

        def _parse_state_schema_version(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        state_schema_version = _parse_state_schema_version(d.pop("state_schema_version", UNSET))

        execution_requirements_dto = cls(
            required_features=required_features,
            required_capabilities=required_capabilities,
            state_schema_version=state_schema_version,
        )

        return execution_requirements_dto
