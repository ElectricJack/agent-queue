from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.render_prompt_response_variables_used import RenderPromptResponseVariablesUsed


T = TypeVar("T", bound="RenderPromptResponse")


@_attrs_define
class RenderPromptResponse:
    """
    Attributes:
        name (str | Unset):  Default: ''.
        rendered (str | Unset):  Default: ''.
        variables_used (RenderPromptResponseVariablesUsed | Unset):
    """

    name: str | Unset = ""
    rendered: str | Unset = ""
    variables_used: RenderPromptResponseVariablesUsed | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        rendered = self.rendered

        variables_used: dict[str, Any] | Unset = UNSET
        if not isinstance(self.variables_used, Unset):
            variables_used = self.variables_used.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if name is not UNSET:
            field_dict["name"] = name
        if rendered is not UNSET:
            field_dict["rendered"] = rendered
        if variables_used is not UNSET:
            field_dict["variables_used"] = variables_used

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.render_prompt_response_variables_used import RenderPromptResponseVariablesUsed

        d = dict(src_dict)
        name = d.pop("name", UNSET)

        rendered = d.pop("rendered", UNSET)

        _variables_used = d.pop("variables_used", UNSET)
        variables_used: RenderPromptResponseVariablesUsed | Unset
        if isinstance(_variables_used, Unset):
            variables_used = UNSET
        else:
            variables_used = RenderPromptResponseVariablesUsed.from_dict(_variables_used)

        render_prompt_response = cls(
            name=name,
            rendered=rendered,
            variables_used=variables_used,
        )

        render_prompt_response.additional_properties = d
        return render_prompt_response

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
