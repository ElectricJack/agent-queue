from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.knowledge_protection_response_authority_type_0 import KnowledgeProtectionResponseAuthorityType0
    from ..models.knowledge_protection_response_cleanup_state_type_0 import KnowledgeProtectionResponseCleanupStateType0
    from ..models.knowledge_protection_response_snapshot_type_0 import KnowledgeProtectionResponseSnapshotType0


T = TypeVar("T", bound="KnowledgeProtectionResponse")


@_attrs_define
class KnowledgeProtectionResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        outcome (None | str | Unset):
        record_id (None | str | Unset):
        replay (bool | Unset):  Default: False.
        content_sha256 (None | str | Unset):
        revision_id (None | str | Unset):
        proposal_id (None | str | Unset):
        proposal_sha256 (None | str | Unset):
        state (None | str | Unset):
        snapshot (KnowledgeProtectionResponseSnapshotType0 | None | Unset):
        authority (KnowledgeProtectionResponseAuthorityType0 | None | Unset):
        redaction_id (None | str | Unset):
        cleanup_state (KnowledgeProtectionResponseCleanupStateType0 | None | Unset):
        dry_run (bool | None | Unset):
    """

    success: bool | Unset = True
    outcome: None | str | Unset = UNSET
    record_id: None | str | Unset = UNSET
    replay: bool | Unset = False
    content_sha256: None | str | Unset = UNSET
    revision_id: None | str | Unset = UNSET
    proposal_id: None | str | Unset = UNSET
    proposal_sha256: None | str | Unset = UNSET
    state: None | str | Unset = UNSET
    snapshot: KnowledgeProtectionResponseSnapshotType0 | None | Unset = UNSET
    authority: KnowledgeProtectionResponseAuthorityType0 | None | Unset = UNSET
    redaction_id: None | str | Unset = UNSET
    cleanup_state: KnowledgeProtectionResponseCleanupStateType0 | None | Unset = UNSET
    dry_run: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.knowledge_protection_response_authority_type_0 import KnowledgeProtectionResponseAuthorityType0
        from ..models.knowledge_protection_response_cleanup_state_type_0 import (
            KnowledgeProtectionResponseCleanupStateType0,
        )
        from ..models.knowledge_protection_response_snapshot_type_0 import KnowledgeProtectionResponseSnapshotType0

        success = self.success

        outcome: None | str | Unset
        if isinstance(self.outcome, Unset):
            outcome = UNSET
        else:
            outcome = self.outcome

        record_id: None | str | Unset
        if isinstance(self.record_id, Unset):
            record_id = UNSET
        else:
            record_id = self.record_id

        replay = self.replay

        content_sha256: None | str | Unset
        if isinstance(self.content_sha256, Unset):
            content_sha256 = UNSET
        else:
            content_sha256 = self.content_sha256

        revision_id: None | str | Unset
        if isinstance(self.revision_id, Unset):
            revision_id = UNSET
        else:
            revision_id = self.revision_id

        proposal_id: None | str | Unset
        if isinstance(self.proposal_id, Unset):
            proposal_id = UNSET
        else:
            proposal_id = self.proposal_id

        proposal_sha256: None | str | Unset
        if isinstance(self.proposal_sha256, Unset):
            proposal_sha256 = UNSET
        else:
            proposal_sha256 = self.proposal_sha256

        state: None | str | Unset
        if isinstance(self.state, Unset):
            state = UNSET
        else:
            state = self.state

        snapshot: dict[str, Any] | None | Unset
        if isinstance(self.snapshot, Unset):
            snapshot = UNSET
        elif isinstance(self.snapshot, KnowledgeProtectionResponseSnapshotType0):
            snapshot = self.snapshot.to_dict()
        else:
            snapshot = self.snapshot

        authority: dict[str, Any] | None | Unset
        if isinstance(self.authority, Unset):
            authority = UNSET
        elif isinstance(self.authority, KnowledgeProtectionResponseAuthorityType0):
            authority = self.authority.to_dict()
        else:
            authority = self.authority

        redaction_id: None | str | Unset
        if isinstance(self.redaction_id, Unset):
            redaction_id = UNSET
        else:
            redaction_id = self.redaction_id

        cleanup_state: dict[str, Any] | None | Unset
        if isinstance(self.cleanup_state, Unset):
            cleanup_state = UNSET
        elif isinstance(self.cleanup_state, KnowledgeProtectionResponseCleanupStateType0):
            cleanup_state = self.cleanup_state.to_dict()
        else:
            cleanup_state = self.cleanup_state

        dry_run: bool | None | Unset
        if isinstance(self.dry_run, Unset):
            dry_run = UNSET
        else:
            dry_run = self.dry_run

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if outcome is not UNSET:
            field_dict["outcome"] = outcome
        if record_id is not UNSET:
            field_dict["record_id"] = record_id
        if replay is not UNSET:
            field_dict["replay"] = replay
        if content_sha256 is not UNSET:
            field_dict["content_sha256"] = content_sha256
        if revision_id is not UNSET:
            field_dict["revision_id"] = revision_id
        if proposal_id is not UNSET:
            field_dict["proposal_id"] = proposal_id
        if proposal_sha256 is not UNSET:
            field_dict["proposal_sha256"] = proposal_sha256
        if state is not UNSET:
            field_dict["state"] = state
        if snapshot is not UNSET:
            field_dict["snapshot"] = snapshot
        if authority is not UNSET:
            field_dict["authority"] = authority
        if redaction_id is not UNSET:
            field_dict["redaction_id"] = redaction_id
        if cleanup_state is not UNSET:
            field_dict["cleanup_state"] = cleanup_state
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.knowledge_protection_response_authority_type_0 import KnowledgeProtectionResponseAuthorityType0
        from ..models.knowledge_protection_response_cleanup_state_type_0 import (
            KnowledgeProtectionResponseCleanupStateType0,
        )
        from ..models.knowledge_protection_response_snapshot_type_0 import KnowledgeProtectionResponseSnapshotType0

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        def _parse_outcome(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        outcome = _parse_outcome(d.pop("outcome", UNSET))

        def _parse_record_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        record_id = _parse_record_id(d.pop("record_id", UNSET))

        replay = d.pop("replay", UNSET)

        def _parse_content_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        content_sha256 = _parse_content_sha256(d.pop("content_sha256", UNSET))

        def _parse_revision_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        revision_id = _parse_revision_id(d.pop("revision_id", UNSET))

        def _parse_proposal_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        proposal_id = _parse_proposal_id(d.pop("proposal_id", UNSET))

        def _parse_proposal_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        proposal_sha256 = _parse_proposal_sha256(d.pop("proposal_sha256", UNSET))

        def _parse_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        state = _parse_state(d.pop("state", UNSET))

        def _parse_snapshot(data: object) -> KnowledgeProtectionResponseSnapshotType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                snapshot_type_0 = KnowledgeProtectionResponseSnapshotType0.from_dict(data)

                return snapshot_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(KnowledgeProtectionResponseSnapshotType0 | None | Unset, data)

        snapshot = _parse_snapshot(d.pop("snapshot", UNSET))

        def _parse_authority(data: object) -> KnowledgeProtectionResponseAuthorityType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                authority_type_0 = KnowledgeProtectionResponseAuthorityType0.from_dict(data)

                return authority_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(KnowledgeProtectionResponseAuthorityType0 | None | Unset, data)

        authority = _parse_authority(d.pop("authority", UNSET))

        def _parse_redaction_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        redaction_id = _parse_redaction_id(d.pop("redaction_id", UNSET))

        def _parse_cleanup_state(data: object) -> KnowledgeProtectionResponseCleanupStateType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                cleanup_state_type_0 = KnowledgeProtectionResponseCleanupStateType0.from_dict(data)

                return cleanup_state_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(KnowledgeProtectionResponseCleanupStateType0 | None | Unset, data)

        cleanup_state = _parse_cleanup_state(d.pop("cleanup_state", UNSET))

        def _parse_dry_run(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        dry_run = _parse_dry_run(d.pop("dry_run", UNSET))

        knowledge_protection_response = cls(
            success=success,
            outcome=outcome,
            record_id=record_id,
            replay=replay,
            content_sha256=content_sha256,
            revision_id=revision_id,
            proposal_id=proposal_id,
            proposal_sha256=proposal_sha256,
            state=state,
            snapshot=snapshot,
            authority=authority,
            redaction_id=redaction_id,
            cleanup_state=cleanup_state,
            dry_run=dry_run,
        )

        knowledge_protection_response.additional_properties = d
        return knowledge_protection_response

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
