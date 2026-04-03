from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.render_prompt_request_variables_type_0 import RenderPromptRequestVariablesType0


T = TypeVar("T", bound="RenderPromptRequest")


@_attrs_define
class RenderPromptRequest:
    """
    Attributes:
        project_id (str): Project ID
        name (str): Template name to render
        variables (None | RenderPromptRequestVariablesType0 | Unset): Key-value pairs for template variables (e.g.
            {"task_title": "Fix login bug"})
    """

    project_id: str
    name: str
    variables: None | RenderPromptRequestVariablesType0 | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.render_prompt_request_variables_type_0 import RenderPromptRequestVariablesType0

        project_id = self.project_id

        name = self.name

        variables: dict[str, Any] | None | Unset
        if isinstance(self.variables, Unset):
            variables = UNSET
        elif isinstance(self.variables, RenderPromptRequestVariablesType0):
            variables = self.variables.to_dict()
        else:
            variables = self.variables

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "name": name,
            }
        )
        if variables is not UNSET:
            field_dict["variables"] = variables

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.render_prompt_request_variables_type_0 import RenderPromptRequestVariablesType0

        d = dict(src_dict)
        project_id = d.pop("project_id")

        name = d.pop("name")

        def _parse_variables(data: object) -> None | RenderPromptRequestVariablesType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                variables_type_0 = RenderPromptRequestVariablesType0.from_dict(data)

                return variables_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RenderPromptRequestVariablesType0 | Unset, data)

        variables = _parse_variables(d.pop("variables", UNSET))

        render_prompt_request = cls(
            project_id=project_id,
            name=name,
            variables=variables,
        )

        render_prompt_request.additional_properties = d
        return render_prompt_request

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
