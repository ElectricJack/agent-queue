from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.agent_status_entry_working_on_type_0 import AgentStatusEntryWorkingOnType0


T = TypeVar("T", bound="AgentStatusEntry")


@_attrs_define
class AgentStatusEntry:
    """
    Attributes:
        workspace_id (str):
        name (str | Unset):  Default: ''.
        project_id (str | Unset):  Default: ''.
        state (str | Unset):  Default: ''.
        working_on (AgentStatusEntryWorkingOnType0 | None | Unset):
    """

    workspace_id: str
    name: str | Unset = ""
    project_id: str | Unset = ""
    state: str | Unset = ""
    working_on: AgentStatusEntryWorkingOnType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_status_entry_working_on_type_0 import AgentStatusEntryWorkingOnType0

        workspace_id = self.workspace_id

        name = self.name

        project_id = self.project_id

        state = self.state

        working_on: dict[str, Any] | None | Unset
        if isinstance(self.working_on, Unset):
            working_on = UNSET
        elif isinstance(self.working_on, AgentStatusEntryWorkingOnType0):
            working_on = self.working_on.to_dict()
        else:
            working_on = self.working_on

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "workspace_id": workspace_id,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if state is not UNSET:
            field_dict["state"] = state
        if working_on is not UNSET:
            field_dict["working_on"] = working_on

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_status_entry_working_on_type_0 import AgentStatusEntryWorkingOnType0

        d = dict(src_dict)
        workspace_id = d.pop("workspace_id")

        name = d.pop("name", UNSET)

        project_id = d.pop("project_id", UNSET)

        state = d.pop("state", UNSET)

        def _parse_working_on(data: object) -> AgentStatusEntryWorkingOnType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                working_on_type_0 = AgentStatusEntryWorkingOnType0.from_dict(data)

                return working_on_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AgentStatusEntryWorkingOnType0 | None | Unset, data)

        working_on = _parse_working_on(d.pop("working_on", UNSET))

        agent_status_entry = cls(
            workspace_id=workspace_id,
            name=name,
            project_id=project_id,
            state=state,
            working_on=working_on,
        )

        agent_status_entry.additional_properties = d
        return agent_status_entry

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
