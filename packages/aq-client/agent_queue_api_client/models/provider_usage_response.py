from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_usage_response_series import ProviderUsageResponseSeries
    from ..models.provider_usage_snapshot import ProviderUsageSnapshot


T = TypeVar("T", bound="ProviderUsageResponse")


@_attrs_define
class ProviderUsageResponse:
    """``GET /api/providers/usage``.

    ``snapshots`` is the newest reading per series --- what the cards render.
    ``series`` is keyed ``"<provider>/<window>/<scope>"`` and is populated only
    when ``?since=`` is given, so the default response stays one row per card.
    ``now`` is the server clock the staleness verdicts were computed against;
    without it a client cannot tell a stale reading from a skewed clock.

        Attributes:
            now (float):
            snapshots (list[ProviderUsageSnapshot] | Unset):
            series (ProviderUsageResponseSeries | Unset):
    """

    now: float
    snapshots: list[ProviderUsageSnapshot] | Unset = UNSET
    series: ProviderUsageResponseSeries | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        now = self.now

        snapshots: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.snapshots, Unset):
            snapshots = []
            for snapshots_item_data in self.snapshots:
                snapshots_item = snapshots_item_data.to_dict()
                snapshots.append(snapshots_item)

        series: dict[str, Any] | Unset = UNSET
        if not isinstance(self.series, Unset):
            series = self.series.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "now": now,
            }
        )
        if snapshots is not UNSET:
            field_dict["snapshots"] = snapshots
        if series is not UNSET:
            field_dict["series"] = series

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_usage_response_series import ProviderUsageResponseSeries
        from ..models.provider_usage_snapshot import ProviderUsageSnapshot

        d = dict(src_dict)
        now = d.pop("now")

        _snapshots = d.pop("snapshots", UNSET)
        snapshots: list[ProviderUsageSnapshot] | Unset = UNSET
        if _snapshots is not UNSET:
            snapshots = []
            for snapshots_item_data in _snapshots:
                snapshots_item = ProviderUsageSnapshot.from_dict(snapshots_item_data)

                snapshots.append(snapshots_item)

        _series = d.pop("series", UNSET)
        series: ProviderUsageResponseSeries | Unset
        if isinstance(_series, Unset):
            series = UNSET
        else:
            series = ProviderUsageResponseSeries.from_dict(_series)

        provider_usage_response = cls(
            now=now,
            snapshots=snapshots,
            series=series,
        )

        provider_usage_response.additional_properties = d
        return provider_usage_response

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
