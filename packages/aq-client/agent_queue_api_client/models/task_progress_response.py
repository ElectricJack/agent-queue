from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.task_progress_response_display_state_type_0 import TaskProgressResponseDisplayStateType0
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.claim_readiness import ClaimReadiness
    from ..models.task_progress_response_child_state_counts_type_0 import TaskProgressResponseChildStateCountsType0
    from ..models.task_progress_response_descendant_state_counts_type_0 import (
        TaskProgressResponseDescendantStateCountsType0,
    )


T = TypeVar("T", bound="TaskProgressResponse")


@_attrs_define
class TaskProgressResponse:
    """
    Attributes:
        success (bool):
        parent_id (str):
        total (int):
        done (int):
        ready (int):
        blocked (int):
        in_progress (int):
        waves (list[list[str]]):
        max_parallelism (int):
        depth (int):
        display_state (None | TaskProgressResponseDisplayStateType0 | Unset):
        display_reason (None | str | Unset):
        child_state_counts (None | TaskProgressResponseChildStateCountsType0 | Unset):
        descendant_state_counts (None | TaskProgressResponseDescendantStateCountsType0 | Unset):
        claim_readiness (ClaimReadiness | None | Unset):
    """

    success: bool
    parent_id: str
    total: int
    done: int
    ready: int
    blocked: int
    in_progress: int
    waves: list[list[str]]
    max_parallelism: int
    depth: int
    display_state: None | TaskProgressResponseDisplayStateType0 | Unset = UNSET
    display_reason: None | str | Unset = UNSET
    child_state_counts: None | TaskProgressResponseChildStateCountsType0 | Unset = UNSET
    descendant_state_counts: None | TaskProgressResponseDescendantStateCountsType0 | Unset = UNSET
    claim_readiness: ClaimReadiness | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.claim_readiness import ClaimReadiness
        from ..models.task_progress_response_child_state_counts_type_0 import TaskProgressResponseChildStateCountsType0
        from ..models.task_progress_response_descendant_state_counts_type_0 import (
            TaskProgressResponseDescendantStateCountsType0,
        )

        success = self.success

        parent_id = self.parent_id

        total = self.total

        done = self.done

        ready = self.ready

        blocked = self.blocked

        in_progress = self.in_progress

        waves = []
        for waves_item_data in self.waves:
            waves_item = waves_item_data

            waves.append(waves_item)

        max_parallelism = self.max_parallelism

        depth = self.depth

        display_state: None | str | Unset
        if isinstance(self.display_state, Unset):
            display_state = UNSET
        elif isinstance(self.display_state, TaskProgressResponseDisplayStateType0):
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
        elif isinstance(self.child_state_counts, TaskProgressResponseChildStateCountsType0):
            child_state_counts = self.child_state_counts.to_dict()
        else:
            child_state_counts = self.child_state_counts

        descendant_state_counts: dict[str, Any] | None | Unset
        if isinstance(self.descendant_state_counts, Unset):
            descendant_state_counts = UNSET
        elif isinstance(self.descendant_state_counts, TaskProgressResponseDescendantStateCountsType0):
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

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
                "parent_id": parent_id,
                "total": total,
                "done": done,
                "ready": ready,
                "blocked": blocked,
                "in_progress": in_progress,
                "waves": waves,
                "max_parallelism": max_parallelism,
                "depth": depth,
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

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.claim_readiness import ClaimReadiness
        from ..models.task_progress_response_child_state_counts_type_0 import TaskProgressResponseChildStateCountsType0
        from ..models.task_progress_response_descendant_state_counts_type_0 import (
            TaskProgressResponseDescendantStateCountsType0,
        )

        d = dict(src_dict)
        success = d.pop("success")

        parent_id = d.pop("parent_id")

        total = d.pop("total")

        done = d.pop("done")

        ready = d.pop("ready")

        blocked = d.pop("blocked")

        in_progress = d.pop("in_progress")

        waves = []
        _waves = d.pop("waves")
        for waves_item_data in _waves:
            waves_item = cast(list[str], waves_item_data)

            waves.append(waves_item)

        max_parallelism = d.pop("max_parallelism")

        depth = d.pop("depth")

        def _parse_display_state(data: object) -> None | TaskProgressResponseDisplayStateType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                display_state_type_0 = TaskProgressResponseDisplayStateType0(data)

                return display_state_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskProgressResponseDisplayStateType0 | Unset, data)

        display_state = _parse_display_state(d.pop("display_state", UNSET))

        def _parse_display_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        display_reason = _parse_display_reason(d.pop("display_reason", UNSET))

        def _parse_child_state_counts(data: object) -> None | TaskProgressResponseChildStateCountsType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                child_state_counts_type_0 = TaskProgressResponseChildStateCountsType0.from_dict(data)

                return child_state_counts_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskProgressResponseChildStateCountsType0 | Unset, data)

        child_state_counts = _parse_child_state_counts(d.pop("child_state_counts", UNSET))

        def _parse_descendant_state_counts(
            data: object,
        ) -> None | TaskProgressResponseDescendantStateCountsType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                descendant_state_counts_type_0 = TaskProgressResponseDescendantStateCountsType0.from_dict(data)

                return descendant_state_counts_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskProgressResponseDescendantStateCountsType0 | Unset, data)

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

        task_progress_response = cls(
            success=success,
            parent_id=parent_id,
            total=total,
            done=done,
            ready=ready,
            blocked=blocked,
            in_progress=in_progress,
            waves=waves,
            max_parallelism=max_parallelism,
            depth=depth,
            display_state=display_state,
            display_reason=display_reason,
            child_state_counts=child_state_counts,
            descendant_state_counts=descendant_state_counts,
            claim_readiness=claim_readiness,
        )

        task_progress_response.additional_properties = d
        return task_progress_response

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
