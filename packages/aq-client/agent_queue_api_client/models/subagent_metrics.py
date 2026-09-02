from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.subagent_metrics_by_session import SubagentMetricsBySession


T = TypeVar("T", bound="SubagentMetrics")


@_attrs_define
class SubagentMetrics:
    """Fleet sub-agent totals plus the per-session drill-down.

    ``complete`` is the conjunction over live sessions: one session without
    hooks makes ``native`` and ``total`` lower bounds for the whole fleet.

        Attributes:
            total (float | Unset):  Default: 0.0.
            native (float | Unset):  Default: 0.0.
            aq (float | Unset):  Default: 0.0.
            complete (bool | Unset):  Default: True.
            by_session (SubagentMetricsBySession | Unset):
    """

    total: float | Unset = 0.0
    native: float | Unset = 0.0
    aq: float | Unset = 0.0
    complete: bool | Unset = True
    by_session: SubagentMetricsBySession | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        total = self.total

        native = self.native

        aq = self.aq

        complete = self.complete

        by_session: dict[str, Any] | Unset = UNSET
        if not isinstance(self.by_session, Unset):
            by_session = self.by_session.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if total is not UNSET:
            field_dict["total"] = total
        if native is not UNSET:
            field_dict["native"] = native
        if aq is not UNSET:
            field_dict["aq"] = aq
        if complete is not UNSET:
            field_dict["complete"] = complete
        if by_session is not UNSET:
            field_dict["by_session"] = by_session

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.subagent_metrics_by_session import SubagentMetricsBySession

        d = dict(src_dict)
        total = d.pop("total", UNSET)

        native = d.pop("native", UNSET)

        aq = d.pop("aq", UNSET)

        complete = d.pop("complete", UNSET)

        _by_session = d.pop("by_session", UNSET)
        by_session: SubagentMetricsBySession | Unset
        if isinstance(_by_session, Unset):
            by_session = UNSET
        else:
            by_session = SubagentMetricsBySession.from_dict(_by_session)

        subagent_metrics = cls(
            total=total,
            native=native,
            aq=aq,
            complete=complete,
            by_session=by_session,
        )

        subagent_metrics.additional_properties = d
        return subagent_metrics

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
