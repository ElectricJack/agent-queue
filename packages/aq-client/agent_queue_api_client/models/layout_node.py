from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.phase_hold_detail import PhaseHoldDetail


T = TypeVar("T", bound="LayoutNode")


@_attrs_define
class LayoutNode:
    """
    Attributes:
        id (str):
        title (str):
        status (str):
        x (float):
        y (float):
        w (float):
        h (float):
        depth (int):
        kind (str):
        priority (int | Unset):  Default: 100.
        is_blocked (bool | Unset):  Default: False.
        profile_id (None | str | Unset):
        intelligence_class (None | str | Unset):
        assigned_agent_id (None | str | Unset):
        branch_name (None | str | Unset):
        pr_url (None | str | Unset):
        playbook_run_id (None | str | Unset):
        container_id (None | str | Unset):
        context_only (bool | Unset):  Default: False.
        agg_children (int | Unset):  Default: 0.
        agg_descendants (int | Unset):  Default: 0.
        agg_completed (int | Unset):  Default: 0.
        agg_running (int | Unset):  Default: 0.
        agg_blocked (int | Unset):  Default: 0.
        agg_active (int | Unset):  Default: 0.
        subtasks_total (int | Unset):  Default: 0.
        subtasks_settled (int | Unset):  Default: 0.
        phase_order (int | None | Unset):
        phase_label (None | str | Unset):
        phase_hold (None | PhaseHoldDetail | Unset):
    """

    id: str
    title: str
    status: str
    x: float
    y: float
    w: float
    h: float
    depth: int
    kind: str
    priority: int | Unset = 100
    is_blocked: bool | Unset = False
    profile_id: None | str | Unset = UNSET
    intelligence_class: None | str | Unset = UNSET
    assigned_agent_id: None | str | Unset = UNSET
    branch_name: None | str | Unset = UNSET
    pr_url: None | str | Unset = UNSET
    playbook_run_id: None | str | Unset = UNSET
    container_id: None | str | Unset = UNSET
    context_only: bool | Unset = False
    agg_children: int | Unset = 0
    agg_descendants: int | Unset = 0
    agg_completed: int | Unset = 0
    agg_running: int | Unset = 0
    agg_blocked: int | Unset = 0
    agg_active: int | Unset = 0
    subtasks_total: int | Unset = 0
    subtasks_settled: int | Unset = 0
    phase_order: int | None | Unset = UNSET
    phase_label: None | str | Unset = UNSET
    phase_hold: None | PhaseHoldDetail | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.phase_hold_detail import PhaseHoldDetail

        id = self.id

        title = self.title

        status = self.status

        x = self.x

        y = self.y

        w = self.w

        h = self.h

        depth = self.depth

        kind = self.kind

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

        container_id: None | str | Unset
        if isinstance(self.container_id, Unset):
            container_id = UNSET
        else:
            container_id = self.container_id

        context_only = self.context_only

        agg_children = self.agg_children

        agg_descendants = self.agg_descendants

        agg_completed = self.agg_completed

        agg_running = self.agg_running

        agg_blocked = self.agg_blocked

        agg_active = self.agg_active

        subtasks_total = self.subtasks_total

        subtasks_settled = self.subtasks_settled

        phase_order: int | None | Unset
        if isinstance(self.phase_order, Unset):
            phase_order = UNSET
        else:
            phase_order = self.phase_order

        phase_label: None | str | Unset
        if isinstance(self.phase_label, Unset):
            phase_label = UNSET
        else:
            phase_label = self.phase_label

        phase_hold: dict[str, Any] | None | Unset
        if isinstance(self.phase_hold, Unset):
            phase_hold = UNSET
        elif isinstance(self.phase_hold, PhaseHoldDetail):
            phase_hold = self.phase_hold.to_dict()
        else:
            phase_hold = self.phase_hold

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "title": title,
                "status": status,
                "x": x,
                "y": y,
                "w": w,
                "h": h,
                "depth": depth,
                "kind": kind,
            }
        )
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
        if container_id is not UNSET:
            field_dict["container_id"] = container_id
        if context_only is not UNSET:
            field_dict["context_only"] = context_only
        if agg_children is not UNSET:
            field_dict["agg_children"] = agg_children
        if agg_descendants is not UNSET:
            field_dict["agg_descendants"] = agg_descendants
        if agg_completed is not UNSET:
            field_dict["agg_completed"] = agg_completed
        if agg_running is not UNSET:
            field_dict["agg_running"] = agg_running
        if agg_blocked is not UNSET:
            field_dict["agg_blocked"] = agg_blocked
        if agg_active is not UNSET:
            field_dict["agg_active"] = agg_active
        if subtasks_total is not UNSET:
            field_dict["subtasks_total"] = subtasks_total
        if subtasks_settled is not UNSET:
            field_dict["subtasks_settled"] = subtasks_settled
        if phase_order is not UNSET:
            field_dict["phase_order"] = phase_order
        if phase_label is not UNSET:
            field_dict["phase_label"] = phase_label
        if phase_hold is not UNSET:
            field_dict["phase_hold"] = phase_hold

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.phase_hold_detail import PhaseHoldDetail

        d = dict(src_dict)
        id = d.pop("id")

        title = d.pop("title")

        status = d.pop("status")

        x = d.pop("x")

        y = d.pop("y")

        w = d.pop("w")

        h = d.pop("h")

        depth = d.pop("depth")

        kind = d.pop("kind")

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

        def _parse_container_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        container_id = _parse_container_id(d.pop("container_id", UNSET))

        context_only = d.pop("context_only", UNSET)

        agg_children = d.pop("agg_children", UNSET)

        agg_descendants = d.pop("agg_descendants", UNSET)

        agg_completed = d.pop("agg_completed", UNSET)

        agg_running = d.pop("agg_running", UNSET)

        agg_blocked = d.pop("agg_blocked", UNSET)

        agg_active = d.pop("agg_active", UNSET)

        subtasks_total = d.pop("subtasks_total", UNSET)

        subtasks_settled = d.pop("subtasks_settled", UNSET)

        def _parse_phase_order(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        phase_order = _parse_phase_order(d.pop("phase_order", UNSET))

        def _parse_phase_label(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        phase_label = _parse_phase_label(d.pop("phase_label", UNSET))

        def _parse_phase_hold(data: object) -> None | PhaseHoldDetail | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                phase_hold_type_0 = PhaseHoldDetail.from_dict(data)

                return phase_hold_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PhaseHoldDetail | Unset, data)

        phase_hold = _parse_phase_hold(d.pop("phase_hold", UNSET))

        layout_node = cls(
            id=id,
            title=title,
            status=status,
            x=x,
            y=y,
            w=w,
            h=h,
            depth=depth,
            kind=kind,
            priority=priority,
            is_blocked=is_blocked,
            profile_id=profile_id,
            intelligence_class=intelligence_class,
            assigned_agent_id=assigned_agent_id,
            branch_name=branch_name,
            pr_url=pr_url,
            playbook_run_id=playbook_run_id,
            container_id=container_id,
            context_only=context_only,
            agg_children=agg_children,
            agg_descendants=agg_descendants,
            agg_completed=agg_completed,
            agg_running=agg_running,
            agg_blocked=agg_blocked,
            agg_active=agg_active,
            subtasks_total=subtasks_total,
            subtasks_settled=subtasks_settled,
            phase_order=phase_order,
            phase_label=phase_label,
            phase_hold=phase_hold,
        )

        layout_node.additional_properties = d
        return layout_node

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
