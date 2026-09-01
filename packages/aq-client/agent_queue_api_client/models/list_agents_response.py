from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.agent_summary import AgentSummary
    from ..models.profile_subagent_rollup import ProfileSubagentRollup
    from ..models.subagent_rollup import SubagentRollup


T = TypeVar("T", bound="ListAgentsResponse")


@_attrs_define
class ListAgentsResponse:
    """
    Attributes:
        agents (list[AgentSummary] | Unset):
        count (int | Unset):  Default: 0.
        project_id (None | str | Unset):
        subagents (None | SubagentRollup | Unset):
        subagents_by_profile (list[ProfileSubagentRollup] | Unset):
    """

    agents: list[AgentSummary] | Unset = UNSET
    count: int | Unset = 0
    project_id: None | str | Unset = UNSET
    subagents: None | SubagentRollup | Unset = UNSET
    subagents_by_profile: list[ProfileSubagentRollup] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.subagent_rollup import SubagentRollup  # noqa: PLC0415

        agents: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.agents, Unset):
            agents = []
            for agents_item_data in self.agents:
                agents_item = agents_item_data.to_dict()
                agents.append(agents_item)

        count = self.count

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        subagents: dict[str, Any] | None | Unset
        if isinstance(self.subagents, Unset):
            subagents = UNSET
        elif isinstance(self.subagents, SubagentRollup):
            subagents = self.subagents.to_dict()
        else:
            subagents = self.subagents

        subagents_by_profile: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.subagents_by_profile, Unset):
            subagents_by_profile = []
            for subagents_by_profile_item_data in self.subagents_by_profile:
                subagents_by_profile_item = subagents_by_profile_item_data.to_dict()
                subagents_by_profile.append(subagents_by_profile_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if agents is not UNSET:
            field_dict["agents"] = agents
        if count is not UNSET:
            field_dict["count"] = count
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if subagents is not UNSET:
            field_dict["subagents"] = subagents
        if subagents_by_profile is not UNSET:
            field_dict["subagents_by_profile"] = subagents_by_profile

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_summary import AgentSummary  # noqa: PLC0415
        from ..models.profile_subagent_rollup import ProfileSubagentRollup  # noqa: PLC0415
        from ..models.subagent_rollup import SubagentRollup  # noqa: PLC0415

        d = dict(src_dict)
        _agents = d.pop("agents", UNSET)
        agents: list[AgentSummary] | Unset = UNSET
        if _agents is not UNSET:
            agents = []
            for agents_item_data in _agents:
                agents_item = AgentSummary.from_dict(agents_item_data)

                agents.append(agents_item)

        count = d.pop("count", UNSET)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_subagents(data: object) -> None | SubagentRollup | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                subagents_type_0 = SubagentRollup.from_dict(data)

                return subagents_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | SubagentRollup | Unset, data)

        subagents = _parse_subagents(d.pop("subagents", UNSET))

        _subagents_by_profile = d.pop("subagents_by_profile", UNSET)
        subagents_by_profile: list[ProfileSubagentRollup] | Unset = UNSET
        if _subagents_by_profile is not UNSET:
            subagents_by_profile = []
            for subagents_by_profile_item_data in _subagents_by_profile:
                subagents_by_profile_item = ProfileSubagentRollup.from_dict(subagents_by_profile_item_data)

                subagents_by_profile.append(subagents_by_profile_item)

        list_agents_response = cls(
            agents=agents,
            count=count,
            project_id=project_id,
            subagents=subagents,
            subagents_by_profile=subagents_by_profile,
        )

        list_agents_response.additional_properties = d
        return list_agents_response

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
