from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.test_selection_observe_request_payload import TestSelectionObserveRequestPayload


T = TypeVar("T", bound="TestSelectionObserveRequest")


@_attrs_define
class TestSelectionObserveRequest:
    """
    Attributes:
        selection_id (str):
        exit_code (int):
        duration_ms (int):
        executed_modules (list[Any]):
        failed_node_ids (list[Any] | Unset):
        payload (TestSelectionObserveRequestPayload | Unset):
    """

    selection_id: str
    exit_code: int
    duration_ms: int
    executed_modules: list[Any]
    failed_node_ids: list[Any] | Unset = UNSET
    payload: TestSelectionObserveRequestPayload | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        selection_id = self.selection_id

        exit_code = self.exit_code

        duration_ms = self.duration_ms

        executed_modules = self.executed_modules

        failed_node_ids: list[Any] | Unset = UNSET
        if not isinstance(self.failed_node_ids, Unset):
            failed_node_ids = self.failed_node_ids

        payload: dict[str, Any] | Unset = UNSET
        if not isinstance(self.payload, Unset):
            payload = self.payload.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "selection_id": selection_id,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
                "executed_modules": executed_modules,
            }
        )
        if failed_node_ids is not UNSET:
            field_dict["failed_node_ids"] = failed_node_ids
        if payload is not UNSET:
            field_dict["payload"] = payload

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.test_selection_observe_request_payload import TestSelectionObserveRequestPayload

        d = dict(src_dict)
        selection_id = d.pop("selection_id")

        exit_code = d.pop("exit_code")

        duration_ms = d.pop("duration_ms")

        executed_modules = cast(list[Any], d.pop("executed_modules"))

        failed_node_ids = cast(list[Any], d.pop("failed_node_ids", UNSET))

        _payload = d.pop("payload", UNSET)
        payload: TestSelectionObserveRequestPayload | Unset
        if isinstance(_payload, Unset):
            payload = UNSET
        else:
            payload = TestSelectionObserveRequestPayload.from_dict(_payload)

        test_selection_observe_request = cls(
            selection_id=selection_id,
            exit_code=exit_code,
            duration_ms=duration_ms,
            executed_modules=executed_modules,
            failed_node_ids=failed_node_ids,
            payload=payload,
        )

        test_selection_observe_request.additional_properties = d
        return test_selection_observe_request

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
