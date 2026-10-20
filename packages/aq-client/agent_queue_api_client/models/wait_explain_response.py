from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.wait_explain_response_explanation_type_0 import WaitExplainResponseExplanationType0
    from ..models.wait_explain_response_waits_item import WaitExplainResponseWaitsItem


T = TypeVar("T", bound="WaitExplainResponse")


@_attrs_define
class WaitExplainResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        explanation (None | Unset | WaitExplainResponseExplanationType0):
        waits (list[WaitExplainResponseWaitsItem] | Unset):
    """

    success: bool | Unset = True
    explanation: None | Unset | WaitExplainResponseExplanationType0 = UNSET
    waits: list[WaitExplainResponseWaitsItem] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.wait_explain_response_explanation_type_0 import WaitExplainResponseExplanationType0

        success = self.success

        explanation: dict[str, Any] | None | Unset
        if isinstance(self.explanation, Unset):
            explanation = UNSET
        elif isinstance(self.explanation, WaitExplainResponseExplanationType0):
            explanation = self.explanation.to_dict()
        else:
            explanation = self.explanation

        waits: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.waits, Unset):
            waits = []
            for waits_item_data in self.waits:
                waits_item = waits_item_data.to_dict()
                waits.append(waits_item)

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if explanation is not UNSET:
            field_dict["explanation"] = explanation
        if waits is not UNSET:
            field_dict["waits"] = waits

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.wait_explain_response_explanation_type_0 import WaitExplainResponseExplanationType0
        from ..models.wait_explain_response_waits_item import WaitExplainResponseWaitsItem

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        def _parse_explanation(data: object) -> None | Unset | WaitExplainResponseExplanationType0:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                explanation_type_0 = WaitExplainResponseExplanationType0.from_dict(data)

                return explanation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitExplainResponseExplanationType0, data)

        explanation = _parse_explanation(d.pop("explanation", UNSET))

        _waits = d.pop("waits", UNSET)
        waits: list[WaitExplainResponseWaitsItem] | Unset = UNSET
        if _waits is not UNSET:
            waits = []
            for waits_item_data in _waits:
                waits_item = WaitExplainResponseWaitsItem.from_dict(waits_item_data)

                waits.append(waits_item)

        wait_explain_response = cls(
            success=success,
            explanation=explanation,
            waits=waits,
        )

        return wait_explain_response
