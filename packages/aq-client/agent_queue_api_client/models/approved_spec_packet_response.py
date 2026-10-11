from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.approved_spec_packet_response_feedback_item import ApprovedSpecPacketResponseFeedbackItem
    from ..models.approved_spec_packet_response_packet import ApprovedSpecPacketResponsePacket


T = TypeVar("T", bound="ApprovedSpecPacketResponse")


@_attrs_define
class ApprovedSpecPacketResponse:
    """
    Attributes:
        project_id (str):
        decision_id (str):
        packet_sha256 (str):
        review_id (None | str):
        revision (int | None):
        snapshot_id (None | str):
        spec_kind (str):
        spec_path (str):
        content_sha256 (str):
        body (str):
        packet (ApprovedSpecPacketResponsePacket):
        feedback (list[ApprovedSpecPacketResponseFeedbackItem]):
        success (bool | Unset):  Default: True.
    """

    project_id: str
    decision_id: str
    packet_sha256: str
    review_id: None | str
    revision: int | None
    snapshot_id: None | str
    spec_kind: str
    spec_path: str
    content_sha256: str
    body: str
    packet: ApprovedSpecPacketResponsePacket
    feedback: list[ApprovedSpecPacketResponseFeedbackItem]
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        decision_id = self.decision_id

        packet_sha256 = self.packet_sha256

        review_id: None | str
        review_id = self.review_id

        revision: int | None
        revision = self.revision

        snapshot_id: None | str
        snapshot_id = self.snapshot_id

        spec_kind = self.spec_kind

        spec_path = self.spec_path

        content_sha256 = self.content_sha256

        body = self.body

        packet = self.packet.to_dict()

        feedback = []
        for feedback_item_data in self.feedback:
            feedback_item = feedback_item_data.to_dict()
            feedback.append(feedback_item)

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "decision_id": decision_id,
                "packet_sha256": packet_sha256,
                "review_id": review_id,
                "revision": revision,
                "snapshot_id": snapshot_id,
                "spec_kind": spec_kind,
                "spec_path": spec_path,
                "content_sha256": content_sha256,
                "body": body,
                "packet": packet,
                "feedback": feedback,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.approved_spec_packet_response_feedback_item import ApprovedSpecPacketResponseFeedbackItem
        from ..models.approved_spec_packet_response_packet import ApprovedSpecPacketResponsePacket

        d = dict(src_dict)
        project_id = d.pop("project_id")

        decision_id = d.pop("decision_id")

        packet_sha256 = d.pop("packet_sha256")

        def _parse_review_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        review_id = _parse_review_id(d.pop("review_id"))

        def _parse_revision(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        revision = _parse_revision(d.pop("revision"))

        def _parse_snapshot_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        snapshot_id = _parse_snapshot_id(d.pop("snapshot_id"))

        spec_kind = d.pop("spec_kind")

        spec_path = d.pop("spec_path")

        content_sha256 = d.pop("content_sha256")

        body = d.pop("body")

        packet = ApprovedSpecPacketResponsePacket.from_dict(d.pop("packet"))

        feedback = []
        _feedback = d.pop("feedback")
        for feedback_item_data in _feedback:
            feedback_item = ApprovedSpecPacketResponseFeedbackItem.from_dict(feedback_item_data)

            feedback.append(feedback_item)

        success = d.pop("success", UNSET)

        approved_spec_packet_response = cls(
            project_id=project_id,
            decision_id=decision_id,
            packet_sha256=packet_sha256,
            review_id=review_id,
            revision=revision,
            snapshot_id=snapshot_id,
            spec_kind=spec_kind,
            spec_path=spec_path,
            content_sha256=content_sha256,
            body=body,
            packet=packet,
            feedback=feedback,
            success=success,
        )

        approved_spec_packet_response.additional_properties = d
        return approved_spec_packet_response

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
