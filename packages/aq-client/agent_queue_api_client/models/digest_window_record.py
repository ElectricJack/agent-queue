from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DigestWindowRecord")


@_attrs_define
class DigestWindowRecord:
    """
    Attributes:
        id (str):
        window_start (float):
        window_end (float):
        send_status (str):
        config_generation (int):
        suppression_reason (None | str | Unset):
        is_catchup (bool | Unset):  Default: False.
        attempt_count (int | Unset):  Default: 0.
    """

    id: str
    window_start: float
    window_end: float
    send_status: str
    config_generation: int
    suppression_reason: None | str | Unset = UNSET
    is_catchup: bool | Unset = False
    attempt_count: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        window_start = self.window_start

        window_end = self.window_end

        send_status = self.send_status

        config_generation = self.config_generation

        suppression_reason: None | str | Unset
        if isinstance(self.suppression_reason, Unset):
            suppression_reason = UNSET
        else:
            suppression_reason = self.suppression_reason

        is_catchup = self.is_catchup

        attempt_count = self.attempt_count

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "window_start": window_start,
                "window_end": window_end,
                "send_status": send_status,
                "config_generation": config_generation,
            }
        )
        if suppression_reason is not UNSET:
            field_dict["suppression_reason"] = suppression_reason
        if is_catchup is not UNSET:
            field_dict["is_catchup"] = is_catchup
        if attempt_count is not UNSET:
            field_dict["attempt_count"] = attempt_count

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        window_start = d.pop("window_start")

        window_end = d.pop("window_end")

        send_status = d.pop("send_status")

        config_generation = d.pop("config_generation")

        def _parse_suppression_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        suppression_reason = _parse_suppression_reason(d.pop("suppression_reason", UNSET))

        is_catchup = d.pop("is_catchup", UNSET)

        attempt_count = d.pop("attempt_count", UNSET)

        digest_window_record = cls(
            id=id,
            window_start=window_start,
            window_end=window_end,
            send_status=send_status,
            config_generation=config_generation,
            suppression_reason=suppression_reason,
            is_catchup=is_catchup,
            attempt_count=attempt_count,
        )

        digest_window_record.additional_properties = d
        return digest_window_record

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
