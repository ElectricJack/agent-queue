from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.delivery_latency_batch_day import DeliveryLatencyBatchDay
    from ..models.delivery_latency_excluded import DeliveryLatencyExcluded
    from ..models.delivery_latency_generation import DeliveryLatencyGeneration
    from ..models.delivery_latency_histogram_bucket import DeliveryLatencyHistogramBucket
    from ..models.delivery_latency_segment import DeliveryLatencySegment
    from ..models.delivery_latency_target import DeliveryLatencyTarget


T = TypeVar("T", bound="DeliveryLatencyResponse")


@_attrs_define
class DeliveryLatencyResponse:
    """``GET /api/metrics/delivery-latency`` — one completion cohort.

    The headline (``metric``) is approval to first sight on dev, minus linked
    human-wait time; the completion-to-dev figures sit beside it. Percentiles
    cover delivered samples only (``percentiles_exclude_pending``), so read
    them with the pending, unknown and excluded counts. Null means no data,
    never zero. ``cache_hit_rate`` is derived from ``rebuild_seconds == 0``.
    No field declares the SLO met.

        Attributes:
            observed_at (float):
            window (str):
            since (float):
            until (float):
            project_id (None | str | Unset):
            metric (str | Unset):  Default: 'approval_to_dev'.
            percentiles_exclude_pending (bool | Unset):  Default: True.
            median_seconds (float | None | Unset):
            p90_seconds (float | None | Unset):
            completion_to_dev_median_seconds (float | None | Unset):
            completion_to_dev_p90_seconds (float | None | Unset):
            sample_count (int | Unset):  Default: 0.
            eligible_count (int | Unset):  Default: 0.
            pending_count (int | Unset):  Default: 0.
            unknown_count (int | Unset):  Default: 0.
            historical_unknown_count (int | Unset):  Default: 0.
            left_censored_count (int | Unset):  Default: 0.
            slow_count (int | Unset):  Default: 0.
            stuck_count (int | Unset):  Default: 0.
            late_sample_count (int | Unset):  Default: 0.
            clock_skew_count (int | Unset):  Default: 0.
            current_blocker_count (int | Unset):  Default: 0.
            excluded (list[DeliveryLatencyExcluded] | Unset):
            oldest_pending_age_seconds (float | None | Unset):
            within_target_fraction (float | None | Unset):
            coverage (float | None | Unset):
            unknown_fraction (float | None | Unset):
            target (DeliveryLatencyTarget | None | Unset):
            targets (list[DeliveryLatencyTarget] | Unset):
            segments (list[DeliveryLatencySegment] | Unset):
            residual_median_seconds (float | None | Unset):
            rebuild_seconds_total (float | None | Unset):
            cache_hit_rate (float | None | Unset):
            cache_hit_rate_derived (bool | Unset):  Default: True.
            parent_delay_median_seconds (float | None | Unset):
            author_merge_median_seconds (float | None | Unset):
            historical_age_histogram (list[DeliveryLatencyHistogramBucket] | Unset):
            batches_per_day (list[DeliveryLatencyBatchDay] | Unset):
            generations (list[DeliveryLatencyGeneration] | Unset):
    """

    observed_at: float
    window: str
    since: float
    until: float
    project_id: None | str | Unset = UNSET
    metric: str | Unset = "approval_to_dev"
    percentiles_exclude_pending: bool | Unset = True
    median_seconds: float | None | Unset = UNSET
    p90_seconds: float | None | Unset = UNSET
    completion_to_dev_median_seconds: float | None | Unset = UNSET
    completion_to_dev_p90_seconds: float | None | Unset = UNSET
    sample_count: int | Unset = 0
    eligible_count: int | Unset = 0
    pending_count: int | Unset = 0
    unknown_count: int | Unset = 0
    historical_unknown_count: int | Unset = 0
    left_censored_count: int | Unset = 0
    slow_count: int | Unset = 0
    stuck_count: int | Unset = 0
    late_sample_count: int | Unset = 0
    clock_skew_count: int | Unset = 0
    current_blocker_count: int | Unset = 0
    excluded: list[DeliveryLatencyExcluded] | Unset = UNSET
    oldest_pending_age_seconds: float | None | Unset = UNSET
    within_target_fraction: float | None | Unset = UNSET
    coverage: float | None | Unset = UNSET
    unknown_fraction: float | None | Unset = UNSET
    target: DeliveryLatencyTarget | None | Unset = UNSET
    targets: list[DeliveryLatencyTarget] | Unset = UNSET
    segments: list[DeliveryLatencySegment] | Unset = UNSET
    residual_median_seconds: float | None | Unset = UNSET
    rebuild_seconds_total: float | None | Unset = UNSET
    cache_hit_rate: float | None | Unset = UNSET
    cache_hit_rate_derived: bool | Unset = True
    parent_delay_median_seconds: float | None | Unset = UNSET
    author_merge_median_seconds: float | None | Unset = UNSET
    historical_age_histogram: list[DeliveryLatencyHistogramBucket] | Unset = UNSET
    batches_per_day: list[DeliveryLatencyBatchDay] | Unset = UNSET
    generations: list[DeliveryLatencyGeneration] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.delivery_latency_target import DeliveryLatencyTarget

        observed_at = self.observed_at

        window = self.window

        since = self.since

        until = self.until

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        metric = self.metric

        percentiles_exclude_pending = self.percentiles_exclude_pending

        median_seconds: float | None | Unset
        if isinstance(self.median_seconds, Unset):
            median_seconds = UNSET
        else:
            median_seconds = self.median_seconds

        p90_seconds: float | None | Unset
        if isinstance(self.p90_seconds, Unset):
            p90_seconds = UNSET
        else:
            p90_seconds = self.p90_seconds

        completion_to_dev_median_seconds: float | None | Unset
        if isinstance(self.completion_to_dev_median_seconds, Unset):
            completion_to_dev_median_seconds = UNSET
        else:
            completion_to_dev_median_seconds = self.completion_to_dev_median_seconds

        completion_to_dev_p90_seconds: float | None | Unset
        if isinstance(self.completion_to_dev_p90_seconds, Unset):
            completion_to_dev_p90_seconds = UNSET
        else:
            completion_to_dev_p90_seconds = self.completion_to_dev_p90_seconds

        sample_count = self.sample_count

        eligible_count = self.eligible_count

        pending_count = self.pending_count

        unknown_count = self.unknown_count

        historical_unknown_count = self.historical_unknown_count

        left_censored_count = self.left_censored_count

        slow_count = self.slow_count

        stuck_count = self.stuck_count

        late_sample_count = self.late_sample_count

        clock_skew_count = self.clock_skew_count

        current_blocker_count = self.current_blocker_count

        excluded: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.excluded, Unset):
            excluded = []
            for excluded_item_data in self.excluded:
                excluded_item = excluded_item_data.to_dict()
                excluded.append(excluded_item)

        oldest_pending_age_seconds: float | None | Unset
        if isinstance(self.oldest_pending_age_seconds, Unset):
            oldest_pending_age_seconds = UNSET
        else:
            oldest_pending_age_seconds = self.oldest_pending_age_seconds

        within_target_fraction: float | None | Unset
        if isinstance(self.within_target_fraction, Unset):
            within_target_fraction = UNSET
        else:
            within_target_fraction = self.within_target_fraction

        coverage: float | None | Unset
        if isinstance(self.coverage, Unset):
            coverage = UNSET
        else:
            coverage = self.coverage

        unknown_fraction: float | None | Unset
        if isinstance(self.unknown_fraction, Unset):
            unknown_fraction = UNSET
        else:
            unknown_fraction = self.unknown_fraction

        target: dict[str, Any] | None | Unset
        if isinstance(self.target, Unset):
            target = UNSET
        elif isinstance(self.target, DeliveryLatencyTarget):
            target = self.target.to_dict()
        else:
            target = self.target

        targets: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.targets, Unset):
            targets = []
            for targets_item_data in self.targets:
                targets_item = targets_item_data.to_dict()
                targets.append(targets_item)

        segments: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.segments, Unset):
            segments = []
            for segments_item_data in self.segments:
                segments_item = segments_item_data.to_dict()
                segments.append(segments_item)

        residual_median_seconds: float | None | Unset
        if isinstance(self.residual_median_seconds, Unset):
            residual_median_seconds = UNSET
        else:
            residual_median_seconds = self.residual_median_seconds

        rebuild_seconds_total: float | None | Unset
        if isinstance(self.rebuild_seconds_total, Unset):
            rebuild_seconds_total = UNSET
        else:
            rebuild_seconds_total = self.rebuild_seconds_total

        cache_hit_rate: float | None | Unset
        if isinstance(self.cache_hit_rate, Unset):
            cache_hit_rate = UNSET
        else:
            cache_hit_rate = self.cache_hit_rate

        cache_hit_rate_derived = self.cache_hit_rate_derived

        parent_delay_median_seconds: float | None | Unset
        if isinstance(self.parent_delay_median_seconds, Unset):
            parent_delay_median_seconds = UNSET
        else:
            parent_delay_median_seconds = self.parent_delay_median_seconds

        author_merge_median_seconds: float | None | Unset
        if isinstance(self.author_merge_median_seconds, Unset):
            author_merge_median_seconds = UNSET
        else:
            author_merge_median_seconds = self.author_merge_median_seconds

        historical_age_histogram: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.historical_age_histogram, Unset):
            historical_age_histogram = []
            for historical_age_histogram_item_data in self.historical_age_histogram:
                historical_age_histogram_item = historical_age_histogram_item_data.to_dict()
                historical_age_histogram.append(historical_age_histogram_item)

        batches_per_day: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.batches_per_day, Unset):
            batches_per_day = []
            for batches_per_day_item_data in self.batches_per_day:
                batches_per_day_item = batches_per_day_item_data.to_dict()
                batches_per_day.append(batches_per_day_item)

        generations: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.generations, Unset):
            generations = []
            for generations_item_data in self.generations:
                generations_item = generations_item_data.to_dict()
                generations.append(generations_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "observed_at": observed_at,
                "window": window,
                "since": since,
                "until": until,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if metric is not UNSET:
            field_dict["metric"] = metric
        if percentiles_exclude_pending is not UNSET:
            field_dict["percentiles_exclude_pending"] = percentiles_exclude_pending
        if median_seconds is not UNSET:
            field_dict["median_seconds"] = median_seconds
        if p90_seconds is not UNSET:
            field_dict["p90_seconds"] = p90_seconds
        if completion_to_dev_median_seconds is not UNSET:
            field_dict["completion_to_dev_median_seconds"] = completion_to_dev_median_seconds
        if completion_to_dev_p90_seconds is not UNSET:
            field_dict["completion_to_dev_p90_seconds"] = completion_to_dev_p90_seconds
        if sample_count is not UNSET:
            field_dict["sample_count"] = sample_count
        if eligible_count is not UNSET:
            field_dict["eligible_count"] = eligible_count
        if pending_count is not UNSET:
            field_dict["pending_count"] = pending_count
        if unknown_count is not UNSET:
            field_dict["unknown_count"] = unknown_count
        if historical_unknown_count is not UNSET:
            field_dict["historical_unknown_count"] = historical_unknown_count
        if left_censored_count is not UNSET:
            field_dict["left_censored_count"] = left_censored_count
        if slow_count is not UNSET:
            field_dict["slow_count"] = slow_count
        if stuck_count is not UNSET:
            field_dict["stuck_count"] = stuck_count
        if late_sample_count is not UNSET:
            field_dict["late_sample_count"] = late_sample_count
        if clock_skew_count is not UNSET:
            field_dict["clock_skew_count"] = clock_skew_count
        if current_blocker_count is not UNSET:
            field_dict["current_blocker_count"] = current_blocker_count
        if excluded is not UNSET:
            field_dict["excluded"] = excluded
        if oldest_pending_age_seconds is not UNSET:
            field_dict["oldest_pending_age_seconds"] = oldest_pending_age_seconds
        if within_target_fraction is not UNSET:
            field_dict["within_target_fraction"] = within_target_fraction
        if coverage is not UNSET:
            field_dict["coverage"] = coverage
        if unknown_fraction is not UNSET:
            field_dict["unknown_fraction"] = unknown_fraction
        if target is not UNSET:
            field_dict["target"] = target
        if targets is not UNSET:
            field_dict["targets"] = targets
        if segments is not UNSET:
            field_dict["segments"] = segments
        if residual_median_seconds is not UNSET:
            field_dict["residual_median_seconds"] = residual_median_seconds
        if rebuild_seconds_total is not UNSET:
            field_dict["rebuild_seconds_total"] = rebuild_seconds_total
        if cache_hit_rate is not UNSET:
            field_dict["cache_hit_rate"] = cache_hit_rate
        if cache_hit_rate_derived is not UNSET:
            field_dict["cache_hit_rate_derived"] = cache_hit_rate_derived
        if parent_delay_median_seconds is not UNSET:
            field_dict["parent_delay_median_seconds"] = parent_delay_median_seconds
        if author_merge_median_seconds is not UNSET:
            field_dict["author_merge_median_seconds"] = author_merge_median_seconds
        if historical_age_histogram is not UNSET:
            field_dict["historical_age_histogram"] = historical_age_histogram
        if batches_per_day is not UNSET:
            field_dict["batches_per_day"] = batches_per_day
        if generations is not UNSET:
            field_dict["generations"] = generations

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.delivery_latency_batch_day import DeliveryLatencyBatchDay
        from ..models.delivery_latency_excluded import DeliveryLatencyExcluded
        from ..models.delivery_latency_generation import DeliveryLatencyGeneration
        from ..models.delivery_latency_histogram_bucket import DeliveryLatencyHistogramBucket
        from ..models.delivery_latency_segment import DeliveryLatencySegment
        from ..models.delivery_latency_target import DeliveryLatencyTarget

        d = dict(src_dict)
        observed_at = d.pop("observed_at")

        window = d.pop("window")

        since = d.pop("since")

        until = d.pop("until")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        metric = d.pop("metric", UNSET)

        percentiles_exclude_pending = d.pop("percentiles_exclude_pending", UNSET)

        def _parse_median_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        median_seconds = _parse_median_seconds(d.pop("median_seconds", UNSET))

        def _parse_p90_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        p90_seconds = _parse_p90_seconds(d.pop("p90_seconds", UNSET))

        def _parse_completion_to_dev_median_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        completion_to_dev_median_seconds = _parse_completion_to_dev_median_seconds(
            d.pop("completion_to_dev_median_seconds", UNSET)
        )

        def _parse_completion_to_dev_p90_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        completion_to_dev_p90_seconds = _parse_completion_to_dev_p90_seconds(
            d.pop("completion_to_dev_p90_seconds", UNSET)
        )

        sample_count = d.pop("sample_count", UNSET)

        eligible_count = d.pop("eligible_count", UNSET)

        pending_count = d.pop("pending_count", UNSET)

        unknown_count = d.pop("unknown_count", UNSET)

        historical_unknown_count = d.pop("historical_unknown_count", UNSET)

        left_censored_count = d.pop("left_censored_count", UNSET)

        slow_count = d.pop("slow_count", UNSET)

        stuck_count = d.pop("stuck_count", UNSET)

        late_sample_count = d.pop("late_sample_count", UNSET)

        clock_skew_count = d.pop("clock_skew_count", UNSET)

        current_blocker_count = d.pop("current_blocker_count", UNSET)

        _excluded = d.pop("excluded", UNSET)
        excluded: list[DeliveryLatencyExcluded] | Unset = UNSET
        if _excluded is not UNSET:
            excluded = []
            for excluded_item_data in _excluded:
                excluded_item = DeliveryLatencyExcluded.from_dict(excluded_item_data)

                excluded.append(excluded_item)

        def _parse_oldest_pending_age_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        oldest_pending_age_seconds = _parse_oldest_pending_age_seconds(d.pop("oldest_pending_age_seconds", UNSET))

        def _parse_within_target_fraction(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        within_target_fraction = _parse_within_target_fraction(d.pop("within_target_fraction", UNSET))

        def _parse_coverage(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        coverage = _parse_coverage(d.pop("coverage", UNSET))

        def _parse_unknown_fraction(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        unknown_fraction = _parse_unknown_fraction(d.pop("unknown_fraction", UNSET))

        def _parse_target(data: object) -> DeliveryLatencyTarget | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                target_type_0 = DeliveryLatencyTarget.from_dict(data)

                return target_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(DeliveryLatencyTarget | None | Unset, data)

        target = _parse_target(d.pop("target", UNSET))

        _targets = d.pop("targets", UNSET)
        targets: list[DeliveryLatencyTarget] | Unset = UNSET
        if _targets is not UNSET:
            targets = []
            for targets_item_data in _targets:
                targets_item = DeliveryLatencyTarget.from_dict(targets_item_data)

                targets.append(targets_item)

        _segments = d.pop("segments", UNSET)
        segments: list[DeliveryLatencySegment] | Unset = UNSET
        if _segments is not UNSET:
            segments = []
            for segments_item_data in _segments:
                segments_item = DeliveryLatencySegment.from_dict(segments_item_data)

                segments.append(segments_item)

        def _parse_residual_median_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        residual_median_seconds = _parse_residual_median_seconds(d.pop("residual_median_seconds", UNSET))

        def _parse_rebuild_seconds_total(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        rebuild_seconds_total = _parse_rebuild_seconds_total(d.pop("rebuild_seconds_total", UNSET))

        def _parse_cache_hit_rate(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        cache_hit_rate = _parse_cache_hit_rate(d.pop("cache_hit_rate", UNSET))

        cache_hit_rate_derived = d.pop("cache_hit_rate_derived", UNSET)

        def _parse_parent_delay_median_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        parent_delay_median_seconds = _parse_parent_delay_median_seconds(d.pop("parent_delay_median_seconds", UNSET))

        def _parse_author_merge_median_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        author_merge_median_seconds = _parse_author_merge_median_seconds(d.pop("author_merge_median_seconds", UNSET))

        _historical_age_histogram = d.pop("historical_age_histogram", UNSET)
        historical_age_histogram: list[DeliveryLatencyHistogramBucket] | Unset = UNSET
        if _historical_age_histogram is not UNSET:
            historical_age_histogram = []
            for historical_age_histogram_item_data in _historical_age_histogram:
                historical_age_histogram_item = DeliveryLatencyHistogramBucket.from_dict(
                    historical_age_histogram_item_data
                )

                historical_age_histogram.append(historical_age_histogram_item)

        _batches_per_day = d.pop("batches_per_day", UNSET)
        batches_per_day: list[DeliveryLatencyBatchDay] | Unset = UNSET
        if _batches_per_day is not UNSET:
            batches_per_day = []
            for batches_per_day_item_data in _batches_per_day:
                batches_per_day_item = DeliveryLatencyBatchDay.from_dict(batches_per_day_item_data)

                batches_per_day.append(batches_per_day_item)

        _generations = d.pop("generations", UNSET)
        generations: list[DeliveryLatencyGeneration] | Unset = UNSET
        if _generations is not UNSET:
            generations = []
            for generations_item_data in _generations:
                generations_item = DeliveryLatencyGeneration.from_dict(generations_item_data)

                generations.append(generations_item)

        delivery_latency_response = cls(
            observed_at=observed_at,
            window=window,
            since=since,
            until=until,
            project_id=project_id,
            metric=metric,
            percentiles_exclude_pending=percentiles_exclude_pending,
            median_seconds=median_seconds,
            p90_seconds=p90_seconds,
            completion_to_dev_median_seconds=completion_to_dev_median_seconds,
            completion_to_dev_p90_seconds=completion_to_dev_p90_seconds,
            sample_count=sample_count,
            eligible_count=eligible_count,
            pending_count=pending_count,
            unknown_count=unknown_count,
            historical_unknown_count=historical_unknown_count,
            left_censored_count=left_censored_count,
            slow_count=slow_count,
            stuck_count=stuck_count,
            late_sample_count=late_sample_count,
            clock_skew_count=clock_skew_count,
            current_blocker_count=current_blocker_count,
            excluded=excluded,
            oldest_pending_age_seconds=oldest_pending_age_seconds,
            within_target_fraction=within_target_fraction,
            coverage=coverage,
            unknown_fraction=unknown_fraction,
            target=target,
            targets=targets,
            segments=segments,
            residual_median_seconds=residual_median_seconds,
            rebuild_seconds_total=rebuild_seconds_total,
            cache_hit_rate=cache_hit_rate,
            cache_hit_rate_derived=cache_hit_rate_derived,
            parent_delay_median_seconds=parent_delay_median_seconds,
            author_merge_median_seconds=author_merge_median_seconds,
            historical_age_histogram=historical_age_histogram,
            batches_per_day=batches_per_day,
            generations=generations,
        )

        delivery_latency_response.additional_properties = d
        return delivery_latency_response

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
