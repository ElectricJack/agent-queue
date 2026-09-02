from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="OutcomeExplanationDTO")


@_attrs_define
class OutcomeExplanationDTO:
    """One legal outcome of a step and where it goes.

    Attributes:
        outcome (str):
        label (str):
        target_step_id (None | str | Unset):
        target_title (None | str | Unset):
        reserved (bool | Unset):  Default: False.
        terminal_outcome (None | str | Unset):
    """

    outcome: str
    label: str
    target_step_id: None | str | Unset = UNSET
    target_title: None | str | Unset = UNSET
    reserved: bool | Unset = False
    terminal_outcome: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        outcome = self.outcome

        label = self.label

        target_step_id: None | str | Unset
        if isinstance(self.target_step_id, Unset):
            target_step_id = UNSET
        else:
            target_step_id = self.target_step_id

        target_title: None | str | Unset
        if isinstance(self.target_title, Unset):
            target_title = UNSET
        else:
            target_title = self.target_title

        reserved = self.reserved

        terminal_outcome: None | str | Unset
        if isinstance(self.terminal_outcome, Unset):
            terminal_outcome = UNSET
        else:
            terminal_outcome = self.terminal_outcome

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "outcome": outcome,
                "label": label,
            }
        )
        if target_step_id is not UNSET:
            field_dict["target_step_id"] = target_step_id
        if target_title is not UNSET:
            field_dict["target_title"] = target_title
        if reserved is not UNSET:
            field_dict["reserved"] = reserved
        if terminal_outcome is not UNSET:
            field_dict["terminal_outcome"] = terminal_outcome

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        outcome = d.pop("outcome")

        label = d.pop("label")

        def _parse_target_step_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        target_step_id = _parse_target_step_id(d.pop("target_step_id", UNSET))

        def _parse_target_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        target_title = _parse_target_title(d.pop("target_title", UNSET))

        reserved = d.pop("reserved", UNSET)

        def _parse_terminal_outcome(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        terminal_outcome = _parse_terminal_outcome(d.pop("terminal_outcome", UNSET))

        outcome_explanation_dto = cls(
            outcome=outcome,
            label=label,
            target_step_id=target_step_id,
            target_title=target_title,
            reserved=reserved,
            terminal_outcome=terminal_outcome,
        )

        return outcome_explanation_dto
