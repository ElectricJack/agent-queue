from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.wait_explanation import WaitExplanation
    from ..models.wait_graph_chain import WaitGraphChain
    from ..models.wait_graph_project_branch_blockers_item import WaitGraphProjectBranchBlockersItem
    from ..models.wait_graph_project_counts_item import WaitGraphProjectCountsItem
    from ..models.wait_graph_project_deadlocks_item import WaitGraphProjectDeadlocksItem
    from ..models.wait_graph_project_escalated_waits_item import WaitGraphProjectEscalatedWaitsItem
    from ..models.wait_graph_project_held_message_backlog_item import WaitGraphProjectHeldMessageBacklogItem
    from ..models.wait_graph_project_truncated import WaitGraphProjectTruncated


T = TypeVar("T", bound="WaitGraphProject")


@_attrs_define
class WaitGraphProject:
    """
    Attributes:
        project_id (str):
        counts (list[WaitGraphProjectCountsItem] | Unset):
        waits (list[WaitExplanation] | Unset):
        total_waits (int | Unset):  Default: 0.
        escalated_waits (list[WaitGraphProjectEscalatedWaitsItem] | Unset):
        branch_blockers (list[WaitGraphProjectBranchBlockersItem] | Unset):
        held_message_backlog (list[WaitGraphProjectHeldMessageBacklogItem] | Unset):
        longest_chains (list[WaitGraphChain] | Unset):
        deadlocks (list[WaitGraphProjectDeadlocksItem] | Unset):
        truncated (WaitGraphProjectTruncated | Unset):
    """

    project_id: str
    counts: list[WaitGraphProjectCountsItem] | Unset = UNSET
    waits: list[WaitExplanation] | Unset = UNSET
    total_waits: int | Unset = 0
    escalated_waits: list[WaitGraphProjectEscalatedWaitsItem] | Unset = UNSET
    branch_blockers: list[WaitGraphProjectBranchBlockersItem] | Unset = UNSET
    held_message_backlog: list[WaitGraphProjectHeldMessageBacklogItem] | Unset = UNSET
    longest_chains: list[WaitGraphChain] | Unset = UNSET
    deadlocks: list[WaitGraphProjectDeadlocksItem] | Unset = UNSET
    truncated: WaitGraphProjectTruncated | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        counts: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.counts, Unset):
            counts = []
            for counts_item_data in self.counts:
                counts_item = counts_item_data.to_dict()
                counts.append(counts_item)

        waits: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.waits, Unset):
            waits = []
            for waits_item_data in self.waits:
                waits_item = waits_item_data.to_dict()
                waits.append(waits_item)

        total_waits = self.total_waits

        escalated_waits: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.escalated_waits, Unset):
            escalated_waits = []
            for escalated_waits_item_data in self.escalated_waits:
                escalated_waits_item = escalated_waits_item_data.to_dict()
                escalated_waits.append(escalated_waits_item)

        branch_blockers: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.branch_blockers, Unset):
            branch_blockers = []
            for branch_blockers_item_data in self.branch_blockers:
                branch_blockers_item = branch_blockers_item_data.to_dict()
                branch_blockers.append(branch_blockers_item)

        held_message_backlog: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.held_message_backlog, Unset):
            held_message_backlog = []
            for held_message_backlog_item_data in self.held_message_backlog:
                held_message_backlog_item = held_message_backlog_item_data.to_dict()
                held_message_backlog.append(held_message_backlog_item)

        longest_chains: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.longest_chains, Unset):
            longest_chains = []
            for longest_chains_item_data in self.longest_chains:
                longest_chains_item = longest_chains_item_data.to_dict()
                longest_chains.append(longest_chains_item)

        deadlocks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.deadlocks, Unset):
            deadlocks = []
            for deadlocks_item_data in self.deadlocks:
                deadlocks_item = deadlocks_item_data.to_dict()
                deadlocks.append(deadlocks_item)

        truncated: dict[str, Any] | Unset = UNSET
        if not isinstance(self.truncated, Unset):
            truncated = self.truncated.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if counts is not UNSET:
            field_dict["counts"] = counts
        if waits is not UNSET:
            field_dict["waits"] = waits
        if total_waits is not UNSET:
            field_dict["total_waits"] = total_waits
        if escalated_waits is not UNSET:
            field_dict["escalated_waits"] = escalated_waits
        if branch_blockers is not UNSET:
            field_dict["branch_blockers"] = branch_blockers
        if held_message_backlog is not UNSET:
            field_dict["held_message_backlog"] = held_message_backlog
        if longest_chains is not UNSET:
            field_dict["longest_chains"] = longest_chains
        if deadlocks is not UNSET:
            field_dict["deadlocks"] = deadlocks
        if truncated is not UNSET:
            field_dict["truncated"] = truncated

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.wait_explanation import WaitExplanation
        from ..models.wait_graph_chain import WaitGraphChain
        from ..models.wait_graph_project_branch_blockers_item import WaitGraphProjectBranchBlockersItem
        from ..models.wait_graph_project_counts_item import WaitGraphProjectCountsItem
        from ..models.wait_graph_project_deadlocks_item import WaitGraphProjectDeadlocksItem
        from ..models.wait_graph_project_escalated_waits_item import WaitGraphProjectEscalatedWaitsItem
        from ..models.wait_graph_project_held_message_backlog_item import WaitGraphProjectHeldMessageBacklogItem
        from ..models.wait_graph_project_truncated import WaitGraphProjectTruncated

        d = dict(src_dict)
        project_id = d.pop("project_id")

        _counts = d.pop("counts", UNSET)
        counts: list[WaitGraphProjectCountsItem] | Unset = UNSET
        if _counts is not UNSET:
            counts = []
            for counts_item_data in _counts:
                counts_item = WaitGraphProjectCountsItem.from_dict(counts_item_data)

                counts.append(counts_item)

        _waits = d.pop("waits", UNSET)
        waits: list[WaitExplanation] | Unset = UNSET
        if _waits is not UNSET:
            waits = []
            for waits_item_data in _waits:
                waits_item = WaitExplanation.from_dict(waits_item_data)

                waits.append(waits_item)

        total_waits = d.pop("total_waits", UNSET)

        _escalated_waits = d.pop("escalated_waits", UNSET)
        escalated_waits: list[WaitGraphProjectEscalatedWaitsItem] | Unset = UNSET
        if _escalated_waits is not UNSET:
            escalated_waits = []
            for escalated_waits_item_data in _escalated_waits:
                escalated_waits_item = WaitGraphProjectEscalatedWaitsItem.from_dict(escalated_waits_item_data)

                escalated_waits.append(escalated_waits_item)

        _branch_blockers = d.pop("branch_blockers", UNSET)
        branch_blockers: list[WaitGraphProjectBranchBlockersItem] | Unset = UNSET
        if _branch_blockers is not UNSET:
            branch_blockers = []
            for branch_blockers_item_data in _branch_blockers:
                branch_blockers_item = WaitGraphProjectBranchBlockersItem.from_dict(branch_blockers_item_data)

                branch_blockers.append(branch_blockers_item)

        _held_message_backlog = d.pop("held_message_backlog", UNSET)
        held_message_backlog: list[WaitGraphProjectHeldMessageBacklogItem] | Unset = UNSET
        if _held_message_backlog is not UNSET:
            held_message_backlog = []
            for held_message_backlog_item_data in _held_message_backlog:
                held_message_backlog_item = WaitGraphProjectHeldMessageBacklogItem.from_dict(
                    held_message_backlog_item_data
                )

                held_message_backlog.append(held_message_backlog_item)

        _longest_chains = d.pop("longest_chains", UNSET)
        longest_chains: list[WaitGraphChain] | Unset = UNSET
        if _longest_chains is not UNSET:
            longest_chains = []
            for longest_chains_item_data in _longest_chains:
                longest_chains_item = WaitGraphChain.from_dict(longest_chains_item_data)

                longest_chains.append(longest_chains_item)

        _deadlocks = d.pop("deadlocks", UNSET)
        deadlocks: list[WaitGraphProjectDeadlocksItem] | Unset = UNSET
        if _deadlocks is not UNSET:
            deadlocks = []
            for deadlocks_item_data in _deadlocks:
                deadlocks_item = WaitGraphProjectDeadlocksItem.from_dict(deadlocks_item_data)

                deadlocks.append(deadlocks_item)

        _truncated = d.pop("truncated", UNSET)
        truncated: WaitGraphProjectTruncated | Unset
        if isinstance(_truncated, Unset):
            truncated = UNSET
        else:
            truncated = WaitGraphProjectTruncated.from_dict(_truncated)

        wait_graph_project = cls(
            project_id=project_id,
            counts=counts,
            waits=waits,
            total_waits=total_waits,
            escalated_waits=escalated_waits,
            branch_blockers=branch_blockers,
            held_message_backlog=held_message_backlog,
            longest_chains=longest_chains,
            deadlocks=deadlocks,
            truncated=truncated,
        )

        wait_graph_project.additional_properties = d
        return wait_graph_project

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
