from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.wait_bridge_response_result import WaitBridgeResponseResult


T = TypeVar("T", bound="WaitBridgeResponse")


@_attrs_define
class WaitBridgeResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        result (WaitBridgeResponseResult | Unset):
    """

    success: bool | Unset = True
    result: WaitBridgeResponseResult | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        result: dict[str, Any] | Unset = UNSET
        if not isinstance(self.result, Unset):
            result = self.result.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if result is not UNSET:
            field_dict["result"] = result

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.wait_bridge_response_result import WaitBridgeResponseResult

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _result = d.pop("result", UNSET)
        result: WaitBridgeResponseResult | Unset
        if isinstance(_result, Unset):
            result = UNSET
        else:
            result = WaitBridgeResponseResult.from_dict(_result)

        wait_bridge_response = cls(
            success=success,
            result=result,
        )

        return wait_bridge_response
