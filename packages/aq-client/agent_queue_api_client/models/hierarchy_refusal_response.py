from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="HierarchyRefusalResponse")


@_attrs_define
class HierarchyRefusalResponse:
    """The 422 body ``delete_task`` / ``archive_task`` answer a refusal with.

    These refusals are contracts, not prose: a surface branches on ``code``
    and renders the detail keys. ``branch_discard_required`` names the
    ``branches`` it wants a choice about, and ``integration_owned`` names the
    audit ``references`` that make the task permanent. ``extra: allow`` keeps
    the rarer keys (e.g. ``live_descendants``' ``sessions``) on the wire, and
    ``src.api.codegen.DETAILED_ERROR_COMMANDS`` is what stops the generic
    envelope from discarding all of them.

        Attributes:
            error (str):
            success (bool | Unset):  Default: False.
            code (None | str | Unset):
    """

    error: str
    success: bool | Unset = False
    code: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        error = self.error

        success = self.success

        code: None | str | Unset
        if isinstance(self.code, Unset):
            code = UNSET
        else:
            code = self.code

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "error": error,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if code is not UNSET:
            field_dict["code"] = code

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        error = d.pop("error")

        success = d.pop("success", UNSET)

        def _parse_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        code = _parse_code(d.pop("code", UNSET))

        hierarchy_refusal_response = cls(
            error=error,
            success=success,
            code=code,
        )

        hierarchy_refusal_response.additional_properties = d
        return hierarchy_refusal_response

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
