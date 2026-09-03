from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.cutover_event_dto_detail import CutoverEventDTODetail


T = TypeVar("T", bound="CutoverEventDTO")


@_attrs_define
class CutoverEventDTO:
    """One row of the append-only cutover audit.

    Attributes:
        event_id (str):
        kind (str):
        at (float):
        actor (str):
        reason (str):
        detail (CutoverEventDTODetail | Unset):
    """

    event_id: str
    kind: str
    at: float
    actor: str
    reason: str
    detail: CutoverEventDTODetail | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        event_id = self.event_id

        kind = self.kind

        at = self.at

        actor = self.actor

        reason = self.reason

        detail: dict[str, Any] | Unset = UNSET
        if not isinstance(self.detail, Unset):
            detail = self.detail.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "event_id": event_id,
                "kind": kind,
                "at": at,
                "actor": actor,
                "reason": reason,
            }
        )
        if detail is not UNSET:
            field_dict["detail"] = detail

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cutover_event_dto_detail import CutoverEventDTODetail

        d = dict(src_dict)
        event_id = d.pop("event_id")

        kind = d.pop("kind")

        at = d.pop("at")

        actor = d.pop("actor")

        reason = d.pop("reason")

        _detail = d.pop("detail", UNSET)
        detail: CutoverEventDTODetail | Unset
        if isinstance(_detail, Unset):
            detail = UNSET
        else:
            detail = CutoverEventDTODetail.from_dict(_detail)

        cutover_event_dto = cls(
            event_id=event_id,
            kind=kind,
            at=at,
            actor=actor,
            reason=reason,
            detail=detail,
        )

        return cutover_event_dto
