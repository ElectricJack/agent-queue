from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="PlaybookCutoverDrainSignoffRequest")


@_attrs_define
class PlaybookCutoverDrainSignoffRequest:
    """
    Attributes:
        reason (str): Why, at least 10 characters. Stored verbatim in the append-only cutover audit.
        signed_by (str): The attesting human's name, at least 2 characters. Recorded alongside the server-derived actor.
    """

    reason: str
    signed_by: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        reason = self.reason

        signed_by = self.signed_by

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "reason": reason,
                "signed_by": signed_by,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        reason = d.pop("reason")

        signed_by = d.pop("signed_by")

        playbook_cutover_drain_signoff_request = cls(
            reason=reason,
            signed_by=signed_by,
        )

        playbook_cutover_drain_signoff_request.additional_properties = d
        return playbook_cutover_drain_signoff_request

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
