from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ask_decision_preview_packet import AskDecisionPreviewPacket


T = TypeVar("T", bound="AskDecisionPreview")


@_attrs_define
class AskDecisionPreview:
    """
    Attributes:
        id (str):
        ask_id (str):
        decision_generation (int):
        actor_id (str):
        source_fingerprint (str):
        feedback_version (int):
        packet (AskDecisionPreviewPacket):
        packet_sha256 (str):
        created_at (float):
        expires_at (float):
        input_id (None | str | Unset):
    """

    id: str
    ask_id: str
    decision_generation: int
    actor_id: str
    source_fingerprint: str
    feedback_version: int
    packet: AskDecisionPreviewPacket
    packet_sha256: str
    created_at: float
    expires_at: float
    input_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        ask_id = self.ask_id

        decision_generation = self.decision_generation

        actor_id = self.actor_id

        source_fingerprint = self.source_fingerprint

        feedback_version = self.feedback_version

        packet = self.packet.to_dict()

        packet_sha256 = self.packet_sha256

        created_at = self.created_at

        expires_at = self.expires_at

        input_id: None | str | Unset
        if isinstance(self.input_id, Unset):
            input_id = UNSET
        else:
            input_id = self.input_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "ask_id": ask_id,
                "decision_generation": decision_generation,
                "actor_id": actor_id,
                "source_fingerprint": source_fingerprint,
                "feedback_version": feedback_version,
                "packet": packet,
                "packet_sha256": packet_sha256,
                "created_at": created_at,
                "expires_at": expires_at,
            }
        )
        if input_id is not UNSET:
            field_dict["input_id"] = input_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_decision_preview_packet import AskDecisionPreviewPacket

        d = dict(src_dict)
        id = d.pop("id")

        ask_id = d.pop("ask_id")

        decision_generation = d.pop("decision_generation")

        actor_id = d.pop("actor_id")

        source_fingerprint = d.pop("source_fingerprint")

        feedback_version = d.pop("feedback_version")

        packet = AskDecisionPreviewPacket.from_dict(d.pop("packet"))

        packet_sha256 = d.pop("packet_sha256")

        created_at = d.pop("created_at")

        expires_at = d.pop("expires_at")

        def _parse_input_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        input_id = _parse_input_id(d.pop("input_id", UNSET))

        ask_decision_preview = cls(
            id=id,
            ask_id=ask_id,
            decision_generation=decision_generation,
            actor_id=actor_id,
            source_fingerprint=source_fingerprint,
            feedback_version=feedback_version,
            packet=packet,
            packet_sha256=packet_sha256,
            created_at=created_at,
            expires_at=expires_at,
            input_id=input_id,
        )

        ask_decision_preview.additional_properties = d
        return ask_decision_preview

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
