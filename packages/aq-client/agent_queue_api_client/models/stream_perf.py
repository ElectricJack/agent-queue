from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.histogram import Histogram
    from ..models.stream_perf_outcome import StreamPerfOutcome


T = TypeVar("T", bound="StreamPerf")


@_attrs_define
class StreamPerf:
    """Handshake counters and open-stream gauge from the route middleware.

    Attributes:
        handshake (Histogram | None | Unset):
        outcome (StreamPerfOutcome | Unset):
        open_ (float | None | Unset):
    """

    handshake: Histogram | None | Unset = UNSET
    outcome: StreamPerfOutcome | Unset = UNSET
    open_: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.histogram import Histogram

        handshake: dict[str, Any] | None | Unset
        if isinstance(self.handshake, Unset):
            handshake = UNSET
        elif isinstance(self.handshake, Histogram):
            handshake = self.handshake.to_dict()
        else:
            handshake = self.handshake

        outcome: dict[str, Any] | Unset = UNSET
        if not isinstance(self.outcome, Unset):
            outcome = self.outcome.to_dict()

        open_: float | None | Unset
        if isinstance(self.open_, Unset):
            open_ = UNSET
        else:
            open_ = self.open_

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if handshake is not UNSET:
            field_dict["handshake"] = handshake
        if outcome is not UNSET:
            field_dict["outcome"] = outcome
        if open_ is not UNSET:
            field_dict["open"] = open_

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.histogram import Histogram
        from ..models.stream_perf_outcome import StreamPerfOutcome

        d = dict(src_dict)

        def _parse_handshake(data: object) -> Histogram | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                handshake_type_0 = Histogram.from_dict(data)

                return handshake_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(Histogram | None | Unset, data)

        handshake = _parse_handshake(d.pop("handshake", UNSET))

        _outcome = d.pop("outcome", UNSET)
        outcome: StreamPerfOutcome | Unset
        if isinstance(_outcome, Unset):
            outcome = UNSET
        else:
            outcome = StreamPerfOutcome.from_dict(_outcome)

        def _parse_open_(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        open_ = _parse_open_(d.pop("open", UNSET))

        stream_perf = cls(
            handshake=handshake,
            outcome=outcome,
            open_=open_,
        )

        stream_perf.additional_properties = d
        return stream_perf

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
