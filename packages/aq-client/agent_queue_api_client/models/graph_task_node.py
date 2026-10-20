from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.graph_task_node_display_state_type_0 import GraphTaskNodeDisplayStateType0
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.claim_readiness import ClaimReadiness
    from ..models.graph_task_node_child_state_counts_type_0 import GraphTaskNodeChildStateCountsType0
    from ..models.graph_task_node_descendant_state_counts_type_0 import GraphTaskNodeDescendantStateCountsType0


T = TypeVar("T", bound="GraphTaskNode")


@_attrs_define
class GraphTaskNode:
    """
    Attributes:
        id (str):
        title (str):
        status (str):
        display_state (GraphTaskNodeDisplayStateType0 | None | Unset):
        display_reason (None | str | Unset):
        child_state_counts (GraphTaskNodeChildStateCountsType0 | None | Unset):
        descendant_state_counts (GraphTaskNodeDescendantStateCountsType0 | None | Unset):
        claim_readiness (ClaimReadiness | None | Unset):
        priority (int | Unset):  Default: 100.
        is_blocked (bool | Unset):  Default: False.
        profile_id (None | str | Unset):
        intelligence_class (None | str | Unset):
        assigned_agent_id (None | str | Unset):
        branch_name (None | str | Unset):
        pr_url (None | str | Unset):
        playbook_run_id (None | str | Unset):
        parent_task_id (None | str | Unset):
    """

    id: str
    title: str
    status: str
    display_state: GraphTaskNodeDisplayStateType0 | None | Unset = UNSET
    display_reason: None | str | Unset = UNSET
    child_state_counts: GraphTaskNodeChildStateCountsType0 | None | Unset = UNSET
    descendant_state_counts: GraphTaskNodeDescendantStateCountsType0 | None | Unset = UNSET
    claim_readiness: ClaimReadiness | None | Unset = UNSET
    priority: int | Unset = 100
    is_blocked: bool | Unset = False
    profile_id: None | str | Unset = UNSET
    intelligence_class: None | str | Unset = UNSET
    assigned_agent_id: None | str | Unset = UNSET
    branch_name: None | str | Unset = UNSET
    pr_url: None | str | Unset = UNSET
    playbook_run_id: None | str | Unset = UNSET
    parent_task_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.claim_readiness import ClaimReadiness
        from ..models.graph_task_node_child_state_counts_type_0 import GraphTaskNodeChildStateCountsType0
        from ..models.graph_task_node_descendant_state_counts_type_0 import GraphTaskNodeDescendantStateCountsType0

        id = self.id

        title = self.title

        status = self.status

        display_state: None | str | Unset
        if isinstance(self.display_state, Unset):
            display_state = UNSET
        elif isinstance(self.display_state, GraphTaskNodeDisplayStateType0):
            display_state = self.display_state.value
        else:
            display_state = self.display_state

        display_reason: None | str | Unset
        if isinstance(self.display_reason, Unset):
            display_reason = UNSET
        else:
            display_reason = self.display_reason

        child_state_counts: dict[str, Any] | None | Unset
        if isinstance(self.child_state_counts, Unset):
            child_state_counts = UNSET
        elif isinstance(self.child_state_counts, GraphTaskNodeChildStateCountsType0):
            child_state_counts = self.child_state_counts.to_dict()
        else:
            child_state_counts = self.child_state_counts

        descendant_state_counts: dict[str, Any] | None | Unset
        if isinstance(self.descendant_state_counts, Unset):
            descendant_state_counts = UNSET
        elif isinstance(self.descendant_state_counts, GraphTaskNodeDescendantStateCountsType0):
            descendant_state_counts = self.descendant_state_counts.to_dict()
        else:
            descendant_state_counts = self.descendant_state_counts

        claim_readiness: dict[str, Any] | None | Unset
        if isinstance(self.claim_readiness, Unset):
            claim_readiness = UNSET
        elif isinstance(self.claim_readiness, ClaimReadiness):
            claim_readiness = self.claim_readiness.to_dict()
        else:
            claim_readiness = self.claim_readiness

        priority = self.priority

        is_blocked = self.is_blocked

        profile_id: None | str | Unset
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id

        intelligence_class: None | str | Unset
        if isinstance(self.intelligence_class, Unset):
            intelligence_class = UNSET
        else:
            intelligence_class = self.intelligence_class

        assigned_agent_id: None | str | Unset
        if isinstance(self.assigned_agent_id, Unset):
            assigned_agent_id = UNSET
        else:
            assigned_agent_id = self.assigned_agent_id

        branch_name: None | str | Unset
        if isinstance(self.branch_name, Unset):
            branch_name = UNSET
        else:
            branch_name = self.branch_name

        pr_url: None | str | Unset
        if isinstance(self.pr_url, Unset):
            pr_url = UNSET
        else:
            pr_url = self.pr_url

        playbook_run_id: None | str | Unset
        if isinstance(self.playbook_run_id, Unset):
            playbook_run_id = UNSET
        else:
            playbook_run_id = self.playbook_run_id

        parent_task_id: None | str | Unset
        if isinstance(self.parent_task_id, Unset):
            parent_task_id = UNSET
        else:
            parent_task_id = self.parent_task_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "title": title,
                "status": status,
            }
        )
        if display_state is not UNSET:
            field_dict["display_state"] = display_state
        if display_reason is not UNSET:
            field_dict["display_reason"] = display_reason
        if child_state_counts is not UNSET:
            field_dict["child_state_counts"] = child_state_counts
        if descendant_state_counts is not UNSET:
            field_dict["descendant_state_counts"] = descendant_state_counts
        if claim_readiness is not UNSET:
            field_dict["claim_readiness"] = claim_readiness
        if priority is not UNSET:
            field_dict["priority"] = priority
        if is_blocked is not UNSET:
            field_dict["is_blocked"] = is_blocked
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class
        if assigned_agent_id is not UNSET:
            field_dict["assigned_agent_id"] = assigned_agent_id
        if branch_name is not UNSET:
            field_dict["branch_name"] = branch_name
        if pr_url is not UNSET:
            field_dict["pr_url"] = pr_url
        if playbook_run_id is not UNSET:
            field_dict["playbook_run_id"] = playbook_run_id
        if parent_task_id is not UNSET:
            field_dict["parent_task_id"] = parent_task_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.claim_readiness import ClaimReadiness
        from ..models.graph_task_node_child_state_counts_type_0 import GraphTaskNodeChildStateCountsType0
        from ..models.graph_task_node_descendant_state_counts_type_0 import GraphTaskNodeDescendantStateCountsType0

        d = dict(src_dict)
        id = d.pop("id")

        title = d.pop("title")

        status = d.pop("status")

        def _parse_display_state(data: object) -> GraphTaskNodeDisplayStateType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                display_state_type_0 = GraphTaskNodeDisplayStateType0(data)

                return display_state_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(GraphTaskNodeDisplayStateType0 | None | Unset, data)

        display_state = _parse_display_state(d.pop("display_state", UNSET))

        def _parse_display_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        display_reason = _parse_display_reason(d.pop("display_reason", UNSET))

        def _parse_child_state_counts(data: object) -> GraphTaskNodeChildStateCountsType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                child_state_counts_type_0 = GraphTaskNodeChildStateCountsType0.from_dict(data)

                return child_state_counts_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(GraphTaskNodeChildStateCountsType0 | None | Unset, data)

        child_state_counts = _parse_child_state_counts(d.pop("child_state_counts", UNSET))

        def _parse_descendant_state_counts(data: object) -> GraphTaskNodeDescendantStateCountsType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                descendant_state_counts_type_0 = GraphTaskNodeDescendantStateCountsType0.from_dict(data)

                return descendant_state_counts_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(GraphTaskNodeDescendantStateCountsType0 | None | Unset, data)

        descendant_state_counts = _parse_descendant_state_counts(d.pop("descendant_state_counts", UNSET))

        def _parse_claim_readiness(data: object) -> ClaimReadiness | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                claim_readiness_type_0 = ClaimReadiness.from_dict(data)

                return claim_readiness_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ClaimReadiness | None | Unset, data)

        claim_readiness = _parse_claim_readiness(d.pop("claim_readiness", UNSET))

        priority = d.pop("priority", UNSET)

        is_blocked = d.pop("is_blocked", UNSET)

        def _parse_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_id = _parse_profile_id(d.pop("profile_id", UNSET))

        def _parse_intelligence_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        intelligence_class = _parse_intelligence_class(d.pop("intelligence_class", UNSET))

        def _parse_assigned_agent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        assigned_agent_id = _parse_assigned_agent_id(d.pop("assigned_agent_id", UNSET))

        def _parse_branch_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        branch_name = _parse_branch_name(d.pop("branch_name", UNSET))

        def _parse_pr_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        pr_url = _parse_pr_url(d.pop("pr_url", UNSET))

        def _parse_playbook_run_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        playbook_run_id = _parse_playbook_run_id(d.pop("playbook_run_id", UNSET))

        def _parse_parent_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_task_id = _parse_parent_task_id(d.pop("parent_task_id", UNSET))

        graph_task_node = cls(
            id=id,
            title=title,
            status=status,
            display_state=display_state,
            display_reason=display_reason,
            child_state_counts=child_state_counts,
            descendant_state_counts=descendant_state_counts,
            claim_readiness=claim_readiness,
            priority=priority,
            is_blocked=is_blocked,
            profile_id=profile_id,
            intelligence_class=intelligence_class,
            assigned_agent_id=assigned_agent_id,
            branch_name=branch_name,
            pr_url=pr_url,
            playbook_run_id=playbook_run_id,
            parent_task_id=parent_task_id,
        )

        graph_task_node.additional_properties = d
        return graph_task_node

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
