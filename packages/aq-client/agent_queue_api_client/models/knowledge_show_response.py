from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.knowledge_show_response_authority_type_0 import KnowledgeShowResponseAuthorityType0
    from ..models.knowledge_show_response_snapshot import KnowledgeShowResponseSnapshot


T = TypeVar("T", bound="KnowledgeShowResponse")


@_attrs_define
class KnowledgeShowResponse:
    """
    Attributes:
        current_revision_id (str):
        current_sequence (int):
        scope_key (str):
        created_at (str):
        actor_id (str):
        change_kind (str):
        success (bool | Unset):  Default: True.
        outcome (None | str | Unset):
        record_id (None | str | Unset):
        replay (bool | Unset):  Default: False.
        content_sha256 (None | str | Unset):
        revision_id (None | str | Unset):
        kind (str | Unset):  Default: 'knowledge'.
        knowledge_alias (None | str | Unset):
        sequence (int | Unset):  Default: 0.
        hash_version (int | Unset):  Default: 1.
        allowed_actions (list[str] | Unset):
        protection (str | Unset):  Default: 'none'.
        stale (bool | Unset):  Default: False.
        stale_reason (None | str | Unset):
        authority (KnowledgeShowResponseAuthorityType0 | None | Unset):
        snapshot (KnowledgeShowResponseSnapshot | Unset):
    """

    current_revision_id: str
    current_sequence: int
    scope_key: str
    created_at: str
    actor_id: str
    change_kind: str
    success: bool | Unset = True
    outcome: None | str | Unset = UNSET
    record_id: None | str | Unset = UNSET
    replay: bool | Unset = False
    content_sha256: None | str | Unset = UNSET
    revision_id: None | str | Unset = UNSET
    kind: str | Unset = "knowledge"
    knowledge_alias: None | str | Unset = UNSET
    sequence: int | Unset = 0
    hash_version: int | Unset = 1
    allowed_actions: list[str] | Unset = UNSET
    protection: str | Unset = "none"
    stale: bool | Unset = False
    stale_reason: None | str | Unset = UNSET
    authority: KnowledgeShowResponseAuthorityType0 | None | Unset = UNSET
    snapshot: KnowledgeShowResponseSnapshot | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.knowledge_show_response_authority_type_0 import KnowledgeShowResponseAuthorityType0

        current_revision_id = self.current_revision_id

        current_sequence = self.current_sequence

        scope_key = self.scope_key

        created_at = self.created_at

        actor_id = self.actor_id

        change_kind = self.change_kind

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

        kind = self.kind

        knowledge_alias: None | str | Unset
        if isinstance(self.knowledge_alias, Unset):
            knowledge_alias = UNSET
        else:
            knowledge_alias = self.knowledge_alias

        sequence = self.sequence

        hash_version = self.hash_version

        allowed_actions: list[str] | Unset = UNSET
        if not isinstance(self.allowed_actions, Unset):
            allowed_actions = self.allowed_actions

        protection = self.protection

        stale = self.stale

        stale_reason: None | str | Unset
        if isinstance(self.stale_reason, Unset):
            stale_reason = UNSET
        else:
            stale_reason = self.stale_reason

        authority: dict[str, Any] | None | Unset
        if isinstance(self.authority, Unset):
            authority = UNSET
        elif isinstance(self.authority, KnowledgeShowResponseAuthorityType0):
            authority = self.authority.to_dict()
        else:
            authority = self.authority

        snapshot: dict[str, Any] | Unset = UNSET
        if not isinstance(self.snapshot, Unset):
            snapshot = self.snapshot.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "current_revision_id": current_revision_id,
                "current_sequence": current_sequence,
                "scope_key": scope_key,
                "created_at": created_at,
                "actor_id": actor_id,
                "change_kind": change_kind,
            }
        )
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
        if kind is not UNSET:
            field_dict["kind"] = kind
        if knowledge_alias is not UNSET:
            field_dict["knowledge_alias"] = knowledge_alias
        if sequence is not UNSET:
            field_dict["sequence"] = sequence
        if hash_version is not UNSET:
            field_dict["hash_version"] = hash_version
        if allowed_actions is not UNSET:
            field_dict["allowed_actions"] = allowed_actions
        if protection is not UNSET:
            field_dict["protection"] = protection
        if stale is not UNSET:
            field_dict["stale"] = stale
        if stale_reason is not UNSET:
            field_dict["stale_reason"] = stale_reason
        if authority is not UNSET:
            field_dict["authority"] = authority
        if snapshot is not UNSET:
            field_dict["snapshot"] = snapshot

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.knowledge_show_response_authority_type_0 import KnowledgeShowResponseAuthorityType0
        from ..models.knowledge_show_response_snapshot import KnowledgeShowResponseSnapshot

        d = dict(src_dict)
        current_revision_id = d.pop("current_revision_id")

        current_sequence = d.pop("current_sequence")

        scope_key = d.pop("scope_key")

        created_at = d.pop("created_at")

        actor_id = d.pop("actor_id")

        change_kind = d.pop("change_kind")

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

        kind = d.pop("kind", UNSET)

        def _parse_knowledge_alias(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        knowledge_alias = _parse_knowledge_alias(d.pop("knowledge_alias", UNSET))

        sequence = d.pop("sequence", UNSET)

        hash_version = d.pop("hash_version", UNSET)

        allowed_actions = cast(list[str], d.pop("allowed_actions", UNSET))

        protection = d.pop("protection", UNSET)

        stale = d.pop("stale", UNSET)

        def _parse_stale_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        stale_reason = _parse_stale_reason(d.pop("stale_reason", UNSET))

        def _parse_authority(data: object) -> KnowledgeShowResponseAuthorityType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                authority_type_0 = KnowledgeShowResponseAuthorityType0.from_dict(data)

                return authority_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(KnowledgeShowResponseAuthorityType0 | None | Unset, data)

        authority = _parse_authority(d.pop("authority", UNSET))

        _snapshot = d.pop("snapshot", UNSET)
        snapshot: KnowledgeShowResponseSnapshot | Unset
        if isinstance(_snapshot, Unset):
            snapshot = UNSET
        else:
            snapshot = KnowledgeShowResponseSnapshot.from_dict(_snapshot)

        knowledge_show_response = cls(
            current_revision_id=current_revision_id,
            current_sequence=current_sequence,
            scope_key=scope_key,
            created_at=created_at,
            actor_id=actor_id,
            change_kind=change_kind,
            success=success,
            outcome=outcome,
            record_id=record_id,
            replay=replay,
            content_sha256=content_sha256,
            revision_id=revision_id,
            kind=kind,
            knowledge_alias=knowledge_alias,
            sequence=sequence,
            hash_version=hash_version,
            allowed_actions=allowed_actions,
            protection=protection,
            stale=stale,
            stale_reason=stale_reason,
            authority=authority,
            snapshot=snapshot,
        )

        knowledge_show_response.additional_properties = d
        return knowledge_show_response

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
