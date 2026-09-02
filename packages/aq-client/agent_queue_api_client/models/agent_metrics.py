from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.agent_metrics_by_harness import AgentMetricsByHarness
    from ..models.agent_metrics_by_lifecycle import AgentMetricsByLifecycle
    from ..models.agent_metrics_by_profile import AgentMetricsByProfile
    from ..models.agent_metrics_by_state import AgentMetricsByState


T = TypeVar("T", bound="AgentMetrics")


@_attrs_define
class AgentMetrics:
    """Live sessions, split the three ways the tab graphs them.

    Attributes:
        total (int | Unset):  Default: 0.
        by_state (AgentMetricsByState | Unset):
        by_harness (AgentMetricsByHarness | Unset):
        by_profile (AgentMetricsByProfile | Unset):
        by_lifecycle (AgentMetricsByLifecycle | Unset):
    """

    total: int | Unset = 0
    by_state: AgentMetricsByState | Unset = UNSET
    by_harness: AgentMetricsByHarness | Unset = UNSET
    by_profile: AgentMetricsByProfile | Unset = UNSET
    by_lifecycle: AgentMetricsByLifecycle | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        total = self.total

        by_state: dict[str, Any] | Unset = UNSET
        if not isinstance(self.by_state, Unset):
            by_state = self.by_state.to_dict()

        by_harness: dict[str, Any] | Unset = UNSET
        if not isinstance(self.by_harness, Unset):
            by_harness = self.by_harness.to_dict()

        by_profile: dict[str, Any] | Unset = UNSET
        if not isinstance(self.by_profile, Unset):
            by_profile = self.by_profile.to_dict()

        by_lifecycle: dict[str, Any] | Unset = UNSET
        if not isinstance(self.by_lifecycle, Unset):
            by_lifecycle = self.by_lifecycle.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if total is not UNSET:
            field_dict["total"] = total
        if by_state is not UNSET:
            field_dict["by_state"] = by_state
        if by_harness is not UNSET:
            field_dict["by_harness"] = by_harness
        if by_profile is not UNSET:
            field_dict["by_profile"] = by_profile
        if by_lifecycle is not UNSET:
            field_dict["by_lifecycle"] = by_lifecycle

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_metrics_by_harness import AgentMetricsByHarness
        from ..models.agent_metrics_by_lifecycle import AgentMetricsByLifecycle
        from ..models.agent_metrics_by_profile import AgentMetricsByProfile
        from ..models.agent_metrics_by_state import AgentMetricsByState

        d = dict(src_dict)
        total = d.pop("total", UNSET)

        _by_state = d.pop("by_state", UNSET)
        by_state: AgentMetricsByState | Unset
        if isinstance(_by_state, Unset):
            by_state = UNSET
        else:
            by_state = AgentMetricsByState.from_dict(_by_state)

        _by_harness = d.pop("by_harness", UNSET)
        by_harness: AgentMetricsByHarness | Unset
        if isinstance(_by_harness, Unset):
            by_harness = UNSET
        else:
            by_harness = AgentMetricsByHarness.from_dict(_by_harness)

        _by_profile = d.pop("by_profile", UNSET)
        by_profile: AgentMetricsByProfile | Unset
        if isinstance(_by_profile, Unset):
            by_profile = UNSET
        else:
            by_profile = AgentMetricsByProfile.from_dict(_by_profile)

        _by_lifecycle = d.pop("by_lifecycle", UNSET)
        by_lifecycle: AgentMetricsByLifecycle | Unset
        if isinstance(_by_lifecycle, Unset):
            by_lifecycle = UNSET
        else:
            by_lifecycle = AgentMetricsByLifecycle.from_dict(_by_lifecycle)

        agent_metrics = cls(
            total=total,
            by_state=by_state,
            by_harness=by_harness,
            by_profile=by_profile,
            by_lifecycle=by_lifecycle,
        )

        agent_metrics.additional_properties = d
        return agent_metrics

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
