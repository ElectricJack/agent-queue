from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.agent_status_entry import AgentStatusEntry
    from ..models.task_status_summary import TaskStatusSummary


T = TypeVar("T", bound="GetStatusResponse")


@_attrs_define
class GetStatusResponse:
    """
    Attributes:
        projects (int | Unset):  Default: 0.
        agents (list[AgentStatusEntry] | Unset):
        tasks (TaskStatusSummary | Unset):
        orchestrator_paused (bool | Unset):  Default: False.
    """

    projects: int | Unset = 0
    agents: list[AgentStatusEntry] | Unset = UNSET
    tasks: TaskStatusSummary | Unset = UNSET
    orchestrator_paused: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        projects = self.projects

        agents: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.agents, Unset):
            agents = []
            for agents_item_data in self.agents:
                agents_item = agents_item_data.to_dict()
                agents.append(agents_item)

        tasks: dict[str, Any] | Unset = UNSET
        if not isinstance(self.tasks, Unset):
            tasks = self.tasks.to_dict()

        orchestrator_paused = self.orchestrator_paused

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if projects is not UNSET:
            field_dict["projects"] = projects
        if agents is not UNSET:
            field_dict["agents"] = agents
        if tasks is not UNSET:
            field_dict["tasks"] = tasks
        if orchestrator_paused is not UNSET:
            field_dict["orchestrator_paused"] = orchestrator_paused

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_status_entry import AgentStatusEntry
        from ..models.task_status_summary import TaskStatusSummary

        d = dict(src_dict)
        projects = d.pop("projects", UNSET)

        _agents = d.pop("agents", UNSET)
        agents: list[AgentStatusEntry] | Unset = UNSET
        if _agents is not UNSET:
            agents = []
            for agents_item_data in _agents:
                agents_item = AgentStatusEntry.from_dict(agents_item_data)

                agents.append(agents_item)

        _tasks = d.pop("tasks", UNSET)
        tasks: TaskStatusSummary | Unset
        if isinstance(_tasks, Unset):
            tasks = UNSET
        else:
            tasks = TaskStatusSummary.from_dict(_tasks)

        orchestrator_paused = d.pop("orchestrator_paused", UNSET)

        get_status_response = cls(
            projects=projects,
            agents=agents,
            tasks=tasks,
            orchestrator_paused=orchestrator_paused,
        )

        get_status_response.additional_properties = d
        return get_status_response

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
