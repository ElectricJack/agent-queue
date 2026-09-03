from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PlaybookCutoverWindowRehearsalRequest")


@_attrs_define
class PlaybookCutoverWindowRehearsalRequest:
    """
    Attributes:
        reason (str): Why, at least 10 characters. Stored verbatim in the append-only cutover audit.
        dashboard_tti_ms (float | None | Unset): The semantic-tab time-to-interactive measured in the manual scenario
            review, in milliseconds. Optional; the window cannot close until one rehearsal has recorded it.
    """

    reason: str
    dashboard_tti_ms: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        reason = self.reason

        dashboard_tti_ms: float | None | Unset
        if isinstance(self.dashboard_tti_ms, Unset):
            dashboard_tti_ms = UNSET
        else:
            dashboard_tti_ms = self.dashboard_tti_ms

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "reason": reason,
            }
        )
        if dashboard_tti_ms is not UNSET:
            field_dict["dashboard_tti_ms"] = dashboard_tti_ms

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        reason = d.pop("reason")

        def _parse_dashboard_tti_ms(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        dashboard_tti_ms = _parse_dashboard_tti_ms(d.pop("dashboard_tti_ms", UNSET))

        playbook_cutover_window_rehearsal_request = cls(
            reason=reason,
            dashboard_tti_ms=dashboard_tti_ms,
        )

        playbook_cutover_window_rehearsal_request.additional_properties = d
        return playbook_cutover_window_rehearsal_request

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
