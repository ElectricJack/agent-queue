from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.ask_preview_packet import AskPreviewPacket


T = TypeVar("T", bound="AskPreview")


@_attrs_define
class AskPreview:
    """
    Attributes:
        id (str):
        packet (AskPreviewPacket):
        packet_sha256 (str):
        expires_at (float):
    """

    id: str
    packet: AskPreviewPacket
    packet_sha256: str
    expires_at: float
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        packet = self.packet.to_dict()

        packet_sha256 = self.packet_sha256

        expires_at = self.expires_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "packet": packet,
                "packet_sha256": packet_sha256,
                "expires_at": expires_at,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_preview_packet import AskPreviewPacket

        d = dict(src_dict)
        id = d.pop("id")

        packet = AskPreviewPacket.from_dict(d.pop("packet"))

        packet_sha256 = d.pop("packet_sha256")

        expires_at = d.pop("expires_at")

        ask_preview = cls(
            id=id,
            packet=packet,
            packet_sha256=packet_sha256,
            expires_at=expires_at,
        )

        ask_preview.additional_properties = d
        return ask_preview

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
