from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.step_diff_dto_change import StepDiffDTOChange
from ..models.step_diff_dto_step_kind_type_0 import StepDiffDTOStepKindType0
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.field_change_dto import FieldChangeDTO
    from ..models.step_explanation_dto import StepExplanationDTO


T = TypeVar("T", bound="StepDiffDTO")


@_attrs_define
class StepDiffDTO:
    """
    Attributes:
        step_id (str):
        change (StepDiffDTOChange):
        rule_id (None | str | Unset):
        step_kind (None | StepDiffDTOStepKindType0 | Unset):
        title_before (None | str | Unset):
        title_after (None | str | Unset):
        field_changes (list[FieldChangeDTO] | Unset):
        explanation_before (None | StepExplanationDTO | Unset):
        explanation_after (None | StepExplanationDTO | Unset):
    """

    step_id: str
    change: StepDiffDTOChange
    rule_id: None | str | Unset = UNSET
    step_kind: None | StepDiffDTOStepKindType0 | Unset = UNSET
    title_before: None | str | Unset = UNSET
    title_after: None | str | Unset = UNSET
    field_changes: list[FieldChangeDTO] | Unset = UNSET
    explanation_before: None | StepExplanationDTO | Unset = UNSET
    explanation_after: None | StepExplanationDTO | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.step_explanation_dto import StepExplanationDTO

        step_id = self.step_id

        change = self.change.value

        rule_id: None | str | Unset
        if isinstance(self.rule_id, Unset):
            rule_id = UNSET
        else:
            rule_id = self.rule_id

        step_kind: None | str | Unset
        if isinstance(self.step_kind, Unset):
            step_kind = UNSET
        elif isinstance(self.step_kind, StepDiffDTOStepKindType0):
            step_kind = self.step_kind.value
        else:
            step_kind = self.step_kind

        title_before: None | str | Unset
        if isinstance(self.title_before, Unset):
            title_before = UNSET
        else:
            title_before = self.title_before

        title_after: None | str | Unset
        if isinstance(self.title_after, Unset):
            title_after = UNSET
        else:
            title_after = self.title_after

        field_changes: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.field_changes, Unset):
            field_changes = []
            for field_changes_item_data in self.field_changes:
                field_changes_item = field_changes_item_data.to_dict()
                field_changes.append(field_changes_item)

        explanation_before: dict[str, Any] | None | Unset
        if isinstance(self.explanation_before, Unset):
            explanation_before = UNSET
        elif isinstance(self.explanation_before, StepExplanationDTO):
            explanation_before = self.explanation_before.to_dict()
        else:
            explanation_before = self.explanation_before

        explanation_after: dict[str, Any] | None | Unset
        if isinstance(self.explanation_after, Unset):
            explanation_after = UNSET
        elif isinstance(self.explanation_after, StepExplanationDTO):
            explanation_after = self.explanation_after.to_dict()
        else:
            explanation_after = self.explanation_after

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "step_id": step_id,
                "change": change,
            }
        )
        if rule_id is not UNSET:
            field_dict["rule_id"] = rule_id
        if step_kind is not UNSET:
            field_dict["step_kind"] = step_kind
        if title_before is not UNSET:
            field_dict["title_before"] = title_before
        if title_after is not UNSET:
            field_dict["title_after"] = title_after
        if field_changes is not UNSET:
            field_dict["field_changes"] = field_changes
        if explanation_before is not UNSET:
            field_dict["explanation_before"] = explanation_before
        if explanation_after is not UNSET:
            field_dict["explanation_after"] = explanation_after

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.field_change_dto import FieldChangeDTO
        from ..models.step_explanation_dto import StepExplanationDTO

        d = dict(src_dict)
        step_id = d.pop("step_id")

        change = StepDiffDTOChange(d.pop("change"))

        def _parse_rule_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        rule_id = _parse_rule_id(d.pop("rule_id", UNSET))

        def _parse_step_kind(data: object) -> None | StepDiffDTOStepKindType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                step_kind_type_0 = StepDiffDTOStepKindType0(data)

                return step_kind_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | StepDiffDTOStepKindType0 | Unset, data)

        step_kind = _parse_step_kind(d.pop("step_kind", UNSET))

        def _parse_title_before(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        title_before = _parse_title_before(d.pop("title_before", UNSET))

        def _parse_title_after(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        title_after = _parse_title_after(d.pop("title_after", UNSET))

        _field_changes = d.pop("field_changes", UNSET)
        field_changes: list[FieldChangeDTO] | Unset = UNSET
        if _field_changes is not UNSET:
            field_changes = []
            for field_changes_item_data in _field_changes:
                field_changes_item = FieldChangeDTO.from_dict(field_changes_item_data)

                field_changes.append(field_changes_item)

        def _parse_explanation_before(data: object) -> None | StepExplanationDTO | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                explanation_before_type_0 = StepExplanationDTO.from_dict(data)

                return explanation_before_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | StepExplanationDTO | Unset, data)

        explanation_before = _parse_explanation_before(d.pop("explanation_before", UNSET))

        def _parse_explanation_after(data: object) -> None | StepExplanationDTO | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                explanation_after_type_0 = StepExplanationDTO.from_dict(data)

                return explanation_after_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | StepExplanationDTO | Unset, data)

        explanation_after = _parse_explanation_after(d.pop("explanation_after", UNSET))

        step_diff_dto = cls(
            step_id=step_id,
            change=change,
            rule_id=rule_id,
            step_kind=step_kind,
            title_before=title_before,
            title_after=title_after,
            field_changes=field_changes,
            explanation_before=explanation_before,
            explanation_after=explanation_after,
        )

        return step_diff_dto
