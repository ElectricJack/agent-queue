from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.reflow_failed_scope import ReflowFailedScope


T = TypeVar("T", bound="GraphReflowStatus")


@_attrs_define
class GraphReflowStatus:
    """Payload of a ``graph_reflow_status`` success response (operator diagnostics).

    Attributes:
        queued (int | Unset):  Default: 0.
        running (int | Unset):  Default: 0.
        failed (int | Unset):  Default: 0.
        failed_scopes (list[ReflowFailedScope] | Unset):
    """

    queued: int | Unset = 0
    running: int | Unset = 0
    failed: int | Unset = 0
    failed_scopes: list[ReflowFailedScope] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        queued = self.queued

        running = self.running

        failed = self.failed

        failed_scopes: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.failed_scopes, Unset):
            failed_scopes = []
            for failed_scopes_item_data in self.failed_scopes:
                failed_scopes_item = failed_scopes_item_data.to_dict()
                failed_scopes.append(failed_scopes_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if queued is not UNSET:
            field_dict["queued"] = queued
        if running is not UNSET:
            field_dict["running"] = running
        if failed is not UNSET:
            field_dict["failed"] = failed
        if failed_scopes is not UNSET:
            field_dict["failed_scopes"] = failed_scopes

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.reflow_failed_scope import ReflowFailedScope

        d = dict(src_dict)
        queued = d.pop("queued", UNSET)

        running = d.pop("running", UNSET)

        failed = d.pop("failed", UNSET)

        _failed_scopes = d.pop("failed_scopes", UNSET)
        failed_scopes: list[ReflowFailedScope] | Unset = UNSET
        if _failed_scopes is not UNSET:
            failed_scopes = []
            for failed_scopes_item_data in _failed_scopes:
                failed_scopes_item = ReflowFailedScope.from_dict(failed_scopes_item_data)

                failed_scopes.append(failed_scopes_item)

        graph_reflow_status = cls(
            queued=queued,
            running=running,
            failed=failed,
            failed_scopes=failed_scopes,
        )

        graph_reflow_status.additional_properties = d
        return graph_reflow_status

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
