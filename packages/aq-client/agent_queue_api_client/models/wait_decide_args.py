from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.wait_decide_args_decision import WaitDecideArgsDecision
from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitDecideArgs")


@_attrs_define
class WaitDecideArgs:
    """
    Attributes:
        wait_id (str):
        decision (WaitDecideArgsDecision):
        note (str):
        project_id (None | str | Unset):
        extend_seconds (float | None | Unset):
        to (None | str | Unset):
        expected_generation (int | None | Unset):
    """

    wait_id: str
    decision: WaitDecideArgsDecision
    note: str
    project_id: None | str | Unset = UNSET
    extend_seconds: float | None | Unset = UNSET
    to: None | str | Unset = UNSET
    expected_generation: int | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        wait_id = self.wait_id

        decision = self.decision.value

        note = self.note

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        extend_seconds: float | None | Unset
        if isinstance(self.extend_seconds, Unset):
            extend_seconds = UNSET
        else:
            extend_seconds = self.extend_seconds

        to: None | str | Unset
        if isinstance(self.to, Unset):
            to = UNSET
        else:
            to = self.to

        expected_generation: int | None | Unset
        if isinstance(self.expected_generation, Unset):
            expected_generation = UNSET
        else:
            expected_generation = self.expected_generation

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "wait_id": wait_id,
                "decision": decision,
                "note": note,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if extend_seconds is not UNSET:
            field_dict["extend_seconds"] = extend_seconds
        if to is not UNSET:
            field_dict["to"] = to
        if expected_generation is not UNSET:
            field_dict["expected_generation"] = expected_generation

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        wait_id = d.pop("wait_id")

        decision = WaitDecideArgsDecision(d.pop("decision"))

        note = d.pop("note")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_extend_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        extend_seconds = _parse_extend_seconds(d.pop("extend_seconds", UNSET))

        def _parse_to(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        to = _parse_to(d.pop("to", UNSET))

        def _parse_expected_generation(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        expected_generation = _parse_expected_generation(d.pop("expected_generation", UNSET))

        wait_decide_args = cls(
            wait_id=wait_id,
            decision=decision,
            note=note,
            project_id=project_id,
            extend_seconds=extend_seconds,
            to=to,
            expected_generation=expected_generation,
        )

        return wait_decide_args
