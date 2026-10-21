from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.delivery_latency_generation_segments import DeliveryLatencyGenerationSegments


T = TypeVar("T", bound="DeliveryLatencyGeneration")


@_attrs_define
class DeliveryLatencyGeneration:
    """One completed generation in drill-down.

    ``status`` is ``sample``, ``pending``, ``unknown``, ``historical_unknown``,
    ``left_censored`` or ``excluded``. ``current_blocker`` marks a sample whose
    dev ref no longer contains it (a rewrite after the timing was taken).

        Attributes:
            task_id (str):
            generation_id (str):
            status (str):
            project_id (None | str | Unset):
            exclusion (None | str | Unset):
            completed_at (float | None | Unset):
            anchor_at (float | None | Unset):
            anchor_kind (None | str | Unset):
            on_dev_at (float | None | Unset):
            parent_at (float | None | Unset):
            parent_ref (None | str | Unset):
            dev_ref (None | str | Unset):
            latest_state (None | str | Unset):
            approval_to_dev_seconds (float | None | Unset):
            completion_to_dev_seconds (float | None | Unset):
            age_seconds (float | None | Unset):
            watchdog (None | str | Unset):
            human_wait_seconds (float | None | Unset):
            segments (DeliveryLatencyGenerationSegments | Unset):
            residual_seconds (float | None | Unset):
            rebuild_seconds (float | None | Unset):
            setup_seconds (float | None | Unset):
            cache_hit_rate (float | None | Unset):
            clock_skew (bool | Unset):  Default: False.
            late (bool | Unset):  Default: False.
            current_blocker (bool | Unset):  Default: False.
    """

    task_id: str
    generation_id: str
    status: str
    project_id: None | str | Unset = UNSET
    exclusion: None | str | Unset = UNSET
    completed_at: float | None | Unset = UNSET
    anchor_at: float | None | Unset = UNSET
    anchor_kind: None | str | Unset = UNSET
    on_dev_at: float | None | Unset = UNSET
    parent_at: float | None | Unset = UNSET
    parent_ref: None | str | Unset = UNSET
    dev_ref: None | str | Unset = UNSET
    latest_state: None | str | Unset = UNSET
    approval_to_dev_seconds: float | None | Unset = UNSET
    completion_to_dev_seconds: float | None | Unset = UNSET
    age_seconds: float | None | Unset = UNSET
    watchdog: None | str | Unset = UNSET
    human_wait_seconds: float | None | Unset = UNSET
    segments: DeliveryLatencyGenerationSegments | Unset = UNSET
    residual_seconds: float | None | Unset = UNSET
    rebuild_seconds: float | None | Unset = UNSET
    setup_seconds: float | None | Unset = UNSET
    cache_hit_rate: float | None | Unset = UNSET
    clock_skew: bool | Unset = False
    late: bool | Unset = False
    current_blocker: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        generation_id = self.generation_id

        status = self.status

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        exclusion: None | str | Unset
        if isinstance(self.exclusion, Unset):
            exclusion = UNSET
        else:
            exclusion = self.exclusion

        completed_at: float | None | Unset
        if isinstance(self.completed_at, Unset):
            completed_at = UNSET
        else:
            completed_at = self.completed_at

        anchor_at: float | None | Unset
        if isinstance(self.anchor_at, Unset):
            anchor_at = UNSET
        else:
            anchor_at = self.anchor_at

        anchor_kind: None | str | Unset
        if isinstance(self.anchor_kind, Unset):
            anchor_kind = UNSET
        else:
            anchor_kind = self.anchor_kind

        on_dev_at: float | None | Unset
        if isinstance(self.on_dev_at, Unset):
            on_dev_at = UNSET
        else:
            on_dev_at = self.on_dev_at

        parent_at: float | None | Unset
        if isinstance(self.parent_at, Unset):
            parent_at = UNSET
        else:
            parent_at = self.parent_at

        parent_ref: None | str | Unset
        if isinstance(self.parent_ref, Unset):
            parent_ref = UNSET
        else:
            parent_ref = self.parent_ref

        dev_ref: None | str | Unset
        if isinstance(self.dev_ref, Unset):
            dev_ref = UNSET
        else:
            dev_ref = self.dev_ref

        latest_state: None | str | Unset
        if isinstance(self.latest_state, Unset):
            latest_state = UNSET
        else:
            latest_state = self.latest_state

        approval_to_dev_seconds: float | None | Unset
        if isinstance(self.approval_to_dev_seconds, Unset):
            approval_to_dev_seconds = UNSET
        else:
            approval_to_dev_seconds = self.approval_to_dev_seconds

        completion_to_dev_seconds: float | None | Unset
        if isinstance(self.completion_to_dev_seconds, Unset):
            completion_to_dev_seconds = UNSET
        else:
            completion_to_dev_seconds = self.completion_to_dev_seconds

        age_seconds: float | None | Unset
        if isinstance(self.age_seconds, Unset):
            age_seconds = UNSET
        else:
            age_seconds = self.age_seconds

        watchdog: None | str | Unset
        if isinstance(self.watchdog, Unset):
            watchdog = UNSET
        else:
            watchdog = self.watchdog

        human_wait_seconds: float | None | Unset
        if isinstance(self.human_wait_seconds, Unset):
            human_wait_seconds = UNSET
        else:
            human_wait_seconds = self.human_wait_seconds

        segments: dict[str, Any] | Unset = UNSET
        if not isinstance(self.segments, Unset):
            segments = self.segments.to_dict()

        residual_seconds: float | None | Unset
        if isinstance(self.residual_seconds, Unset):
            residual_seconds = UNSET
        else:
            residual_seconds = self.residual_seconds

        rebuild_seconds: float | None | Unset
        if isinstance(self.rebuild_seconds, Unset):
            rebuild_seconds = UNSET
        else:
            rebuild_seconds = self.rebuild_seconds

        setup_seconds: float | None | Unset
        if isinstance(self.setup_seconds, Unset):
            setup_seconds = UNSET
        else:
            setup_seconds = self.setup_seconds

        cache_hit_rate: float | None | Unset
        if isinstance(self.cache_hit_rate, Unset):
            cache_hit_rate = UNSET
        else:
            cache_hit_rate = self.cache_hit_rate

        clock_skew = self.clock_skew

        late = self.late

        current_blocker = self.current_blocker

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "generation_id": generation_id,
                "status": status,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if exclusion is not UNSET:
            field_dict["exclusion"] = exclusion
        if completed_at is not UNSET:
            field_dict["completed_at"] = completed_at
        if anchor_at is not UNSET:
            field_dict["anchor_at"] = anchor_at
        if anchor_kind is not UNSET:
            field_dict["anchor_kind"] = anchor_kind
        if on_dev_at is not UNSET:
            field_dict["on_dev_at"] = on_dev_at
        if parent_at is not UNSET:
            field_dict["parent_at"] = parent_at
        if parent_ref is not UNSET:
            field_dict["parent_ref"] = parent_ref
        if dev_ref is not UNSET:
            field_dict["dev_ref"] = dev_ref
        if latest_state is not UNSET:
            field_dict["latest_state"] = latest_state
        if approval_to_dev_seconds is not UNSET:
            field_dict["approval_to_dev_seconds"] = approval_to_dev_seconds
        if completion_to_dev_seconds is not UNSET:
            field_dict["completion_to_dev_seconds"] = completion_to_dev_seconds
        if age_seconds is not UNSET:
            field_dict["age_seconds"] = age_seconds
        if watchdog is not UNSET:
            field_dict["watchdog"] = watchdog
        if human_wait_seconds is not UNSET:
            field_dict["human_wait_seconds"] = human_wait_seconds
        if segments is not UNSET:
            field_dict["segments"] = segments
        if residual_seconds is not UNSET:
            field_dict["residual_seconds"] = residual_seconds
        if rebuild_seconds is not UNSET:
            field_dict["rebuild_seconds"] = rebuild_seconds
        if setup_seconds is not UNSET:
            field_dict["setup_seconds"] = setup_seconds
        if cache_hit_rate is not UNSET:
            field_dict["cache_hit_rate"] = cache_hit_rate
        if clock_skew is not UNSET:
            field_dict["clock_skew"] = clock_skew
        if late is not UNSET:
            field_dict["late"] = late
        if current_blocker is not UNSET:
            field_dict["current_blocker"] = current_blocker

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.delivery_latency_generation_segments import DeliveryLatencyGenerationSegments

        d = dict(src_dict)
        task_id = d.pop("task_id")

        generation_id = d.pop("generation_id")

        status = d.pop("status")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_exclusion(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        exclusion = _parse_exclusion(d.pop("exclusion", UNSET))

        def _parse_completed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        completed_at = _parse_completed_at(d.pop("completed_at", UNSET))

        def _parse_anchor_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        anchor_at = _parse_anchor_at(d.pop("anchor_at", UNSET))

        def _parse_anchor_kind(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        anchor_kind = _parse_anchor_kind(d.pop("anchor_kind", UNSET))

        def _parse_on_dev_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        on_dev_at = _parse_on_dev_at(d.pop("on_dev_at", UNSET))

        def _parse_parent_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        parent_at = _parse_parent_at(d.pop("parent_at", UNSET))

        def _parse_parent_ref(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_ref = _parse_parent_ref(d.pop("parent_ref", UNSET))

        def _parse_dev_ref(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        dev_ref = _parse_dev_ref(d.pop("dev_ref", UNSET))

        def _parse_latest_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        latest_state = _parse_latest_state(d.pop("latest_state", UNSET))

        def _parse_approval_to_dev_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        approval_to_dev_seconds = _parse_approval_to_dev_seconds(d.pop("approval_to_dev_seconds", UNSET))

        def _parse_completion_to_dev_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        completion_to_dev_seconds = _parse_completion_to_dev_seconds(d.pop("completion_to_dev_seconds", UNSET))

        def _parse_age_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        age_seconds = _parse_age_seconds(d.pop("age_seconds", UNSET))

        def _parse_watchdog(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        watchdog = _parse_watchdog(d.pop("watchdog", UNSET))

        def _parse_human_wait_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        human_wait_seconds = _parse_human_wait_seconds(d.pop("human_wait_seconds", UNSET))

        _segments = d.pop("segments", UNSET)
        segments: DeliveryLatencyGenerationSegments | Unset
        if isinstance(_segments, Unset):
            segments = UNSET
        else:
            segments = DeliveryLatencyGenerationSegments.from_dict(_segments)

        def _parse_residual_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        residual_seconds = _parse_residual_seconds(d.pop("residual_seconds", UNSET))

        def _parse_rebuild_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        rebuild_seconds = _parse_rebuild_seconds(d.pop("rebuild_seconds", UNSET))

        def _parse_setup_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        setup_seconds = _parse_setup_seconds(d.pop("setup_seconds", UNSET))

        def _parse_cache_hit_rate(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        cache_hit_rate = _parse_cache_hit_rate(d.pop("cache_hit_rate", UNSET))

        clock_skew = d.pop("clock_skew", UNSET)

        late = d.pop("late", UNSET)

        current_blocker = d.pop("current_blocker", UNSET)

        delivery_latency_generation = cls(
            task_id=task_id,
            generation_id=generation_id,
            status=status,
            project_id=project_id,
            exclusion=exclusion,
            completed_at=completed_at,
            anchor_at=anchor_at,
            anchor_kind=anchor_kind,
            on_dev_at=on_dev_at,
            parent_at=parent_at,
            parent_ref=parent_ref,
            dev_ref=dev_ref,
            latest_state=latest_state,
            approval_to_dev_seconds=approval_to_dev_seconds,
            completion_to_dev_seconds=completion_to_dev_seconds,
            age_seconds=age_seconds,
            watchdog=watchdog,
            human_wait_seconds=human_wait_seconds,
            segments=segments,
            residual_seconds=residual_seconds,
            rebuild_seconds=rebuild_seconds,
            setup_seconds=setup_seconds,
            cache_hit_rate=cache_hit_rate,
            clock_skew=clock_skew,
            late=late,
            current_blocker=current_blocker,
        )

        delivery_latency_generation.additional_properties = d
        return delivery_latency_generation

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
