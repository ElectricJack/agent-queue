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
        project_id (None | str | Unset): Project ID (required unless path is set)
        name (None | str | Unset): Template name to render (required unless path is set)
        path (None | str | Unset): Absolute filesystem path to a bundled template, e.g. '/opt/agent-
            queue/src/prompts/consolidation_task.md'. In playbook authoring, use aq://prompts/<name>.md instead — the
            playbook compiler rewrites it to an absolute path. Mutually exclusive with (project_id, name).
        variables (None | RenderPromptRequestVariablesType0 | Unset): Key-value pairs for template variables (e.g.
            {"task_title": "Fix login bug"})
    """

    project_id: None | str | Unset = UNSET
    name: None | str | Unset = UNSET
    path: None | str | Unset = UNSET
    variables: None | RenderPromptRequestVariablesType0 | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.render_prompt_request_variables_type_0 import RenderPromptRequestVariablesType0  # noqa: PLC0415

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        path: None | str | Unset
        if isinstance(self.path, Unset):
            path = UNSET
        else:
            path = self.path

        variables: dict[str, Any] | None | Unset
        if isinstance(self.variables, Unset):
            variables = UNSET
        elif isinstance(self.variables, RenderPromptRequestVariablesType0):
            variables = self.variables.to_dict()
        else:
            variables = self.variables

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if name is not UNSET:
            field_dict["name"] = name
        if path is not UNSET:
            field_dict["path"] = path
        if variables is not UNSET:
            field_dict["variables"] = variables

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.render_prompt_request_variables_type_0 import RenderPromptRequestVariablesType0  # noqa: PLC0415

        d = dict(src_dict)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        def _parse_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        path = _parse_path(d.pop("path", UNSET))

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
            path=path,
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
