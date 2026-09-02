from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.wait_facts_dto_deadline_source_type_0 import WaitFactsDTODeadlineSourceType0
from ..models.wait_facts_dto_wait_kind import WaitFactsDTOWaitKind
from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitFactsDTO")


@_attrs_define
class WaitFactsDTO:
    """
    Attributes:
        wait_kind (WaitFactsDTOWaitKind):
        correlation_key (str):
        registered_at (float):
        deadline_at (float | None | Unset):
        deadline_source (None | Unset | WaitFactsDTODeadlineSourceType0):
        matched_at (float | None | Unset):
        matched_event_id (None | str | Unset):
    """

    wait_kind: WaitFactsDTOWaitKind
    correlation_key: str
    registered_at: float
    deadline_at: float | None | Unset = UNSET
    deadline_source: None | Unset | WaitFactsDTODeadlineSourceType0 = UNSET
    matched_at: float | None | Unset = UNSET
    matched_event_id: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        wait_kind = self.wait_kind.value

        correlation_key = self.correlation_key

        registered_at = self.registered_at

        deadline_at: float | None | Unset
        if isinstance(self.deadline_at, Unset):
            deadline_at = UNSET
        else:
            deadline_at = self.deadline_at

        deadline_source: None | str | Unset
        if isinstance(self.deadline_source, Unset):
            deadline_source = UNSET
        elif isinstance(self.deadline_source, WaitFactsDTODeadlineSourceType0):
            deadline_source = self.deadline_source.value
        else:
            deadline_source = self.deadline_source

        matched_at: float | None | Unset
        if isinstance(self.matched_at, Unset):
            matched_at = UNSET
        else:
            matched_at = self.matched_at

        matched_event_id: None | str | Unset
        if isinstance(self.matched_event_id, Unset):
            matched_event_id = UNSET
        else:
            matched_event_id = self.matched_event_id

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "wait_kind": wait_kind,
                "correlation_key": correlation_key,
                "registered_at": registered_at,
            }
        )
        if deadline_at is not UNSET:
            field_dict["deadline_at"] = deadline_at
        if deadline_source is not UNSET:
            field_dict["deadline_source"] = deadline_source
        if matched_at is not UNSET:
            field_dict["matched_at"] = matched_at
        if matched_event_id is not UNSET:
            field_dict["matched_event_id"] = matched_event_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        wait_kind = WaitFactsDTOWaitKind(d.pop("wait_kind"))

        correlation_key = d.pop("correlation_key")

        registered_at = d.pop("registered_at")

        def _parse_deadline_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        deadline_at = _parse_deadline_at(d.pop("deadline_at", UNSET))

        def _parse_deadline_source(data: object) -> None | Unset | WaitFactsDTODeadlineSourceType0:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                deadline_source_type_0 = WaitFactsDTODeadlineSourceType0(data)

                return deadline_source_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitFactsDTODeadlineSourceType0, data)

        deadline_source = _parse_deadline_source(d.pop("deadline_source", UNSET))

        def _parse_matched_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        matched_at = _parse_matched_at(d.pop("matched_at", UNSET))

        def _parse_matched_event_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        matched_event_id = _parse_matched_event_id(d.pop("matched_event_id", UNSET))

        wait_facts_dto = cls(
            wait_kind=wait_kind,
            correlation_key=correlation_key,
            registered_at=registered_at,
            deadline_at=deadline_at,
            deadline_source=deadline_source,
            matched_at=matched_at,
            matched_event_id=matched_event_id,
        )

        return wait_facts_dto
