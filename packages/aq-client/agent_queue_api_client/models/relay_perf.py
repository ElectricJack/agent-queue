from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.histogram import Histogram
    from ..models.relay_perf_upstream_failures import RelayPerfUpstreamFailures


T = TypeVar("T", bound="RelayPerf")


@_attrs_define
class RelayPerf:
    """Dashboard-server relay deltas; unavailable until the relay is polled.

    Attributes:
        available (bool | Unset):  Default: False.
        reason (None | str | Unset):
        http (Histogram | None | Unset):
        ws_handshake (Histogram | None | Unset):
        upstream_failures (RelayPerfUpstreamFailures | Unset):
        relays_open (float | None | Unset):
    """

    available: bool | Unset = False
    reason: None | str | Unset = UNSET
    http: Histogram | None | Unset = UNSET
    ws_handshake: Histogram | None | Unset = UNSET
    upstream_failures: RelayPerfUpstreamFailures | Unset = UNSET
    relays_open: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.histogram import Histogram

        available = self.available

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        http: dict[str, Any] | None | Unset
        if isinstance(self.http, Unset):
            http = UNSET
        elif isinstance(self.http, Histogram):
            http = self.http.to_dict()
        else:
            http = self.http

        ws_handshake: dict[str, Any] | None | Unset
        if isinstance(self.ws_handshake, Unset):
            ws_handshake = UNSET
        elif isinstance(self.ws_handshake, Histogram):
            ws_handshake = self.ws_handshake.to_dict()
        else:
            ws_handshake = self.ws_handshake

        upstream_failures: dict[str, Any] | Unset = UNSET
        if not isinstance(self.upstream_failures, Unset):
            upstream_failures = self.upstream_failures.to_dict()

        relays_open: float | None | Unset
        if isinstance(self.relays_open, Unset):
            relays_open = UNSET
        else:
            relays_open = self.relays_open

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if available is not UNSET:
            field_dict["available"] = available
        if reason is not UNSET:
            field_dict["reason"] = reason
        if http is not UNSET:
            field_dict["http"] = http
        if ws_handshake is not UNSET:
            field_dict["ws_handshake"] = ws_handshake
        if upstream_failures is not UNSET:
            field_dict["upstream_failures"] = upstream_failures
        if relays_open is not UNSET:
            field_dict["relays_open"] = relays_open

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.histogram import Histogram
        from ..models.relay_perf_upstream_failures import RelayPerfUpstreamFailures

        d = dict(src_dict)
        available = d.pop("available", UNSET)

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        def _parse_http(data: object) -> Histogram | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                http_type_0 = Histogram.from_dict(data)

                return http_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(Histogram | None | Unset, data)

        http = _parse_http(d.pop("http", UNSET))

        def _parse_ws_handshake(data: object) -> Histogram | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                ws_handshake_type_0 = Histogram.from_dict(data)

                return ws_handshake_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(Histogram | None | Unset, data)

        ws_handshake = _parse_ws_handshake(d.pop("ws_handshake", UNSET))

        _upstream_failures = d.pop("upstream_failures", UNSET)
        upstream_failures: RelayPerfUpstreamFailures | Unset
        if isinstance(_upstream_failures, Unset):
            upstream_failures = UNSET
        else:
            upstream_failures = RelayPerfUpstreamFailures.from_dict(_upstream_failures)

        def _parse_relays_open(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        relays_open = _parse_relays_open(d.pop("relays_open", UNSET))

        relay_perf = cls(
            available=available,
            reason=reason,
            http=http,
            ws_handshake=ws_handshake,
            upstream_failures=upstream_failures,
            relays_open=relays_open,
        )

        relay_perf.additional_properties = d
        return relay_perf

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
