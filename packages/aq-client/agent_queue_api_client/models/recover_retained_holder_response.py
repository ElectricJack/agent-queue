from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.recover_retained_holder_response_checkpoints_item import RecoverRetainedHolderResponseCheckpointsItem
    from ..models.recover_retained_holder_response_evidence_type_0 import RecoverRetainedHolderResponseEvidenceType0
    from ..models.recover_retained_holder_response_snapshot_type_0 import RecoverRetainedHolderResponseSnapshotType0


T = TypeVar("T", bound="RecoverRetainedHolderResponse")


@_attrs_define
class RecoverRetainedHolderResponse:
    """
    Attributes:
        success (bool):
        preview (bool | None | Unset):
        changed (bool | Unset):  Default: False.
        proof (None | str | Unset):
        snapshot (None | RecoverRetainedHolderResponseSnapshotType0 | Unset):
        evidence (None | RecoverRetainedHolderResponseEvidenceType0 | Unset):
        checkpoints (list[RecoverRetainedHolderResponseCheckpointsItem] | Unset):
        event_id (int | None | Unset):
        error_code (None | str | Unset):
        error (None | str | Unset):
    """

    success: bool
    preview: bool | None | Unset = UNSET
    changed: bool | Unset = False
    proof: None | str | Unset = UNSET
    snapshot: None | RecoverRetainedHolderResponseSnapshotType0 | Unset = UNSET
    evidence: None | RecoverRetainedHolderResponseEvidenceType0 | Unset = UNSET
    checkpoints: list[RecoverRetainedHolderResponseCheckpointsItem] | Unset = UNSET
    event_id: int | None | Unset = UNSET
    error_code: None | str | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.recover_retained_holder_response_evidence_type_0 import RecoverRetainedHolderResponseEvidenceType0
        from ..models.recover_retained_holder_response_snapshot_type_0 import RecoverRetainedHolderResponseSnapshotType0

        success = self.success

        preview: bool | None | Unset
        if isinstance(self.preview, Unset):
            preview = UNSET
        else:
            preview = self.preview

        changed = self.changed

        proof: None | str | Unset
        if isinstance(self.proof, Unset):
            proof = UNSET
        else:
            proof = self.proof

        snapshot: dict[str, Any] | None | Unset
        if isinstance(self.snapshot, Unset):
            snapshot = UNSET
        elif isinstance(self.snapshot, RecoverRetainedHolderResponseSnapshotType0):
            snapshot = self.snapshot.to_dict()
        else:
            snapshot = self.snapshot

        evidence: dict[str, Any] | None | Unset
        if isinstance(self.evidence, Unset):
            evidence = UNSET
        elif isinstance(self.evidence, RecoverRetainedHolderResponseEvidenceType0):
            evidence = self.evidence.to_dict()
        else:
            evidence = self.evidence

        checkpoints: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.checkpoints, Unset):
            checkpoints = []
            for checkpoints_item_data in self.checkpoints:
                checkpoints_item = checkpoints_item_data.to_dict()
                checkpoints.append(checkpoints_item)

        event_id: int | None | Unset
        if isinstance(self.event_id, Unset):
            event_id = UNSET
        else:
            event_id = self.event_id

        error_code: None | str | Unset
        if isinstance(self.error_code, Unset):
            error_code = UNSET
        else:
            error_code = self.error_code

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
            }
        )
        if preview is not UNSET:
            field_dict["preview"] = preview
        if changed is not UNSET:
            field_dict["changed"] = changed
        if proof is not UNSET:
            field_dict["proof"] = proof
        if snapshot is not UNSET:
            field_dict["snapshot"] = snapshot
        if evidence is not UNSET:
            field_dict["evidence"] = evidence
        if checkpoints is not UNSET:
            field_dict["checkpoints"] = checkpoints
        if event_id is not UNSET:
            field_dict["event_id"] = event_id
        if error_code is not UNSET:
            field_dict["error_code"] = error_code
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recover_retained_holder_response_checkpoints_item import (
            RecoverRetainedHolderResponseCheckpointsItem,
        )
        from ..models.recover_retained_holder_response_evidence_type_0 import RecoverRetainedHolderResponseEvidenceType0
        from ..models.recover_retained_holder_response_snapshot_type_0 import RecoverRetainedHolderResponseSnapshotType0

        d = dict(src_dict)
        success = d.pop("success")

        def _parse_preview(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        preview = _parse_preview(d.pop("preview", UNSET))

        changed = d.pop("changed", UNSET)

        def _parse_proof(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        proof = _parse_proof(d.pop("proof", UNSET))

        def _parse_snapshot(data: object) -> None | RecoverRetainedHolderResponseSnapshotType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                snapshot_type_0 = RecoverRetainedHolderResponseSnapshotType0.from_dict(data)

                return snapshot_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecoverRetainedHolderResponseSnapshotType0 | Unset, data)

        snapshot = _parse_snapshot(d.pop("snapshot", UNSET))

        def _parse_evidence(data: object) -> None | RecoverRetainedHolderResponseEvidenceType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                evidence_type_0 = RecoverRetainedHolderResponseEvidenceType0.from_dict(data)

                return evidence_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RecoverRetainedHolderResponseEvidenceType0 | Unset, data)

        evidence = _parse_evidence(d.pop("evidence", UNSET))

        _checkpoints = d.pop("checkpoints", UNSET)
        checkpoints: list[RecoverRetainedHolderResponseCheckpointsItem] | Unset = UNSET
        if _checkpoints is not UNSET:
            checkpoints = []
            for checkpoints_item_data in _checkpoints:
                checkpoints_item = RecoverRetainedHolderResponseCheckpointsItem.from_dict(checkpoints_item_data)

                checkpoints.append(checkpoints_item)

        def _parse_event_id(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        event_id = _parse_event_id(d.pop("event_id", UNSET))

        def _parse_error_code(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error_code = _parse_error_code(d.pop("error_code", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        recover_retained_holder_response = cls(
            success=success,
            preview=preview,
            changed=changed,
            proof=proof,
            snapshot=snapshot,
            evidence=evidence,
            checkpoints=checkpoints,
            event_id=event_id,
            error_code=error_code,
            error=error,
        )

        recover_retained_holder_response.additional_properties = d
        return recover_retained_holder_response

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
