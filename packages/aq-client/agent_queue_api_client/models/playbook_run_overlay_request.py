from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PlaybookRunOverlayRequest")


@_attrs_define
class PlaybookRunOverlayRequest:
    """
    Attributes:
        run_id (str): The V2 run to overlay.
        receipt_limit (int | Unset): Max receipts returned, newest first (default 500). 'truncated' reports when more
            exist; receipts are never silently dropped. Default: 500.
    """

    run_id: str
    receipt_limit: int | Unset = 500
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        run_id = self.run_id

        receipt_limit = self.receipt_limit

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "run_id": run_id,
            }
        )
        if receipt_limit is not UNSET:
            field_dict["receipt_limit"] = receipt_limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        run_id = d.pop("run_id")

        receipt_limit = d.pop("receipt_limit", UNSET)

        playbook_run_overlay_request = cls(
            run_id=run_id,
            receipt_limit=receipt_limit,
        )

        playbook_run_overlay_request.additional_properties = d
        return playbook_run_overlay_request

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
