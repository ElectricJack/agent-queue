from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.object_checkpoint_read_response_evidence import ObjectCheckpointReadResponseEvidence
    from ..models.object_checkpoint_read_response_state import ObjectCheckpointReadResponseState


T = TypeVar("T", bound="ObjectCheckpointReadResponse")


@_attrs_define
class ObjectCheckpointReadResponse:
    """
    Attributes:
        object_id (str):
        version (int):
        state (ObjectCheckpointReadResponseState):
        approved (bool):
        evidence (ObjectCheckpointReadResponseEvidence):
        success (bool | Unset):  Default: True.
    """

    object_id: str
    version: int
    state: ObjectCheckpointReadResponseState
    approved: bool
    evidence: ObjectCheckpointReadResponseEvidence
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        object_id = self.object_id

        version = self.version

        state = self.state.to_dict()

        approved = self.approved

        evidence = self.evidence.to_dict()

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "object_id": object_id,
                "version": version,
                "state": state,
                "approved": approved,
                "evidence": evidence,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.object_checkpoint_read_response_evidence import ObjectCheckpointReadResponseEvidence
        from ..models.object_checkpoint_read_response_state import ObjectCheckpointReadResponseState

        d = dict(src_dict)
        object_id = d.pop("object_id")

        version = d.pop("version")

        state = ObjectCheckpointReadResponseState.from_dict(d.pop("state"))

        approved = d.pop("approved")

        evidence = ObjectCheckpointReadResponseEvidence.from_dict(d.pop("evidence"))

        success = d.pop("success", UNSET)

        object_checkpoint_read_response = cls(
            object_id=object_id,
            version=version,
            state=state,
            approved=approved,
            evidence=evidence,
            success=success,
        )

        object_checkpoint_read_response.additional_properties = d
        return object_checkpoint_read_response

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
