from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.wait_explanation import WaitExplanation
    from ..models.wait_task_explanation import WaitTaskExplanation


T = TypeVar("T", bound="WaitExplainResponse")


@_attrs_define
class WaitExplainResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        explanation (None | Unset | WaitExplanation):
        waits (list[WaitExplanation] | Unset):
        task (None | Unset | WaitTaskExplanation):
        total (int | Unset):  Default: 0.
        offset (int | Unset):  Default: 0.
        truncated (bool | Unset):  Default: False.
    """

    success: bool | Unset = True
    explanation: None | Unset | WaitExplanation = UNSET
    waits: list[WaitExplanation] | Unset = UNSET
    task: None | Unset | WaitTaskExplanation = UNSET
    total: int | Unset = 0
    offset: int | Unset = 0
    truncated: bool | Unset = False

    def to_dict(self) -> dict[str, Any]:
        from ..models.wait_explanation import WaitExplanation
        from ..models.wait_task_explanation import WaitTaskExplanation

        success = self.success

        explanation: dict[str, Any] | None | Unset
        if isinstance(self.explanation, Unset):
            explanation = UNSET
        elif isinstance(self.explanation, WaitExplanation):
            explanation = self.explanation.to_dict()
        else:
            explanation = self.explanation

        waits: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.waits, Unset):
            waits = []
            for waits_item_data in self.waits:
                waits_item = waits_item_data.to_dict()
                waits.append(waits_item)

        task: dict[str, Any] | None | Unset
        if isinstance(self.task, Unset):
            task = UNSET
        elif isinstance(self.task, WaitTaskExplanation):
            task = self.task.to_dict()
        else:
            task = self.task

        total = self.total

        offset = self.offset

        truncated = self.truncated

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if explanation is not UNSET:
            field_dict["explanation"] = explanation
        if waits is not UNSET:
            field_dict["waits"] = waits
        if task is not UNSET:
            field_dict["task"] = task
        if total is not UNSET:
            field_dict["total"] = total
        if offset is not UNSET:
            field_dict["offset"] = offset
        if truncated is not UNSET:
            field_dict["truncated"] = truncated

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.wait_explanation import WaitExplanation
        from ..models.wait_task_explanation import WaitTaskExplanation

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        def _parse_explanation(data: object) -> None | Unset | WaitExplanation:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                explanation_type_0 = WaitExplanation.from_dict(data)

                return explanation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitExplanation, data)

        explanation = _parse_explanation(d.pop("explanation", UNSET))

        _waits = d.pop("waits", UNSET)
        waits: list[WaitExplanation] | Unset = UNSET
        if _waits is not UNSET:
            waits = []
            for waits_item_data in _waits:
                waits_item = WaitExplanation.from_dict(waits_item_data)

                waits.append(waits_item)

        def _parse_task(data: object) -> None | Unset | WaitTaskExplanation:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                task_type_0 = WaitTaskExplanation.from_dict(data)

                return task_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitTaskExplanation, data)

        task = _parse_task(d.pop("task", UNSET))

        total = d.pop("total", UNSET)

        offset = d.pop("offset", UNSET)

        truncated = d.pop("truncated", UNSET)

        wait_explain_response = cls(
            success=success,
            explanation=explanation,
            waits=waits,
            task=task,
            total=total,
            offset=offset,
            truncated=truncated,
        )

        return wait_explain_response
