from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.knowledge_update_request_metadata_type_0 import KnowledgeUpdateRequestMetadataType0


T = TypeVar("T", bound="KnowledgeUpdateRequest")


@_attrs_define
class KnowledgeUpdateRequest:
    """
    Attributes:
        identity (str):
        idempotency_key (str):
        project_id (None | str | Unset):
        if_revision (None | str | Unset):
        claim_epoch (int | None | Unset):
        title (None | str | Unset):
        body (None | str | Unset):
        category (None | str | Unset):
        summary (None | str | Unset):
        tags (list[Any] | None | Unset):
        sources (list[Any] | None | Unset):
        metadata (KnowledgeUpdateRequestMetadataType0 | None | Unset):
        change_reason (None | str | Unset):
        valid_from (None | str | Unset):
        valid_until (None | str | Unset):
        recheck_at (None | str | Unset):
        summary_of_revision (None | str | Unset):
        global_scope (bool | Unset):  Default: False.
    """

    identity: str
    idempotency_key: str
    project_id: None | str | Unset = UNSET
    if_revision: None | str | Unset = UNSET
    claim_epoch: int | None | Unset = UNSET
    title: None | str | Unset = UNSET
    body: None | str | Unset = UNSET
    category: None | str | Unset = UNSET
    summary: None | str | Unset = UNSET
    tags: list[Any] | None | Unset = UNSET
    sources: list[Any] | None | Unset = UNSET
    metadata: KnowledgeUpdateRequestMetadataType0 | None | Unset = UNSET
    change_reason: None | str | Unset = UNSET
    valid_from: None | str | Unset = UNSET
    valid_until: None | str | Unset = UNSET
    recheck_at: None | str | Unset = UNSET
    summary_of_revision: None | str | Unset = UNSET
    global_scope: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.knowledge_update_request_metadata_type_0 import KnowledgeUpdateRequestMetadataType0

        identity = self.identity

        idempotency_key = self.idempotency_key

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        if_revision: None | str | Unset
        if isinstance(self.if_revision, Unset):
            if_revision = UNSET
        else:
            if_revision = self.if_revision

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        title: None | str | Unset
        if isinstance(self.title, Unset):
            title = UNSET
        else:
            title = self.title

        body: None | str | Unset
        if isinstance(self.body, Unset):
            body = UNSET
        else:
            body = self.body

        category: None | str | Unset
        if isinstance(self.category, Unset):
            category = UNSET
        else:
            category = self.category

        summary: None | str | Unset
        if isinstance(self.summary, Unset):
            summary = UNSET
        else:
            summary = self.summary

        tags: list[Any] | None | Unset
        if isinstance(self.tags, Unset):
            tags = UNSET
        elif isinstance(self.tags, list):
            tags = self.tags

        else:
            tags = self.tags

        sources: list[Any] | None | Unset
        if isinstance(self.sources, Unset):
            sources = UNSET
        elif isinstance(self.sources, list):
            sources = self.sources

        else:
            sources = self.sources

        metadata: dict[str, Any] | None | Unset
        if isinstance(self.metadata, Unset):
            metadata = UNSET
        elif isinstance(self.metadata, KnowledgeUpdateRequestMetadataType0):
            metadata = self.metadata.to_dict()
        else:
            metadata = self.metadata

        change_reason: None | str | Unset
        if isinstance(self.change_reason, Unset):
            change_reason = UNSET
        else:
            change_reason = self.change_reason

        valid_from: None | str | Unset
        if isinstance(self.valid_from, Unset):
            valid_from = UNSET
        else:
            valid_from = self.valid_from

        valid_until: None | str | Unset
        if isinstance(self.valid_until, Unset):
            valid_until = UNSET
        else:
            valid_until = self.valid_until

        recheck_at: None | str | Unset
        if isinstance(self.recheck_at, Unset):
            recheck_at = UNSET
        else:
            recheck_at = self.recheck_at

        summary_of_revision: None | str | Unset
        if isinstance(self.summary_of_revision, Unset):
            summary_of_revision = UNSET
        else:
            summary_of_revision = self.summary_of_revision

        global_scope = self.global_scope

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "identity": identity,
                "idempotency_key": idempotency_key,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if if_revision is not UNSET:
            field_dict["if_revision"] = if_revision
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch
        if title is not UNSET:
            field_dict["title"] = title
        if body is not UNSET:
            field_dict["body"] = body
        if category is not UNSET:
            field_dict["category"] = category
        if summary is not UNSET:
            field_dict["summary"] = summary
        if tags is not UNSET:
            field_dict["tags"] = tags
        if sources is not UNSET:
            field_dict["sources"] = sources
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if change_reason is not UNSET:
            field_dict["change_reason"] = change_reason
        if valid_from is not UNSET:
            field_dict["valid_from"] = valid_from
        if valid_until is not UNSET:
            field_dict["valid_until"] = valid_until
        if recheck_at is not UNSET:
            field_dict["recheck_at"] = recheck_at
        if summary_of_revision is not UNSET:
            field_dict["summary_of_revision"] = summary_of_revision
        if global_scope is not UNSET:
            field_dict["global_scope"] = global_scope

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.knowledge_update_request_metadata_type_0 import KnowledgeUpdateRequestMetadataType0

        d = dict(src_dict)
        identity = d.pop("identity")

        idempotency_key = d.pop("idempotency_key")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_if_revision(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        if_revision = _parse_if_revision(d.pop("if_revision", UNSET))

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        def _parse_title(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        title = _parse_title(d.pop("title", UNSET))

        def _parse_body(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        body = _parse_body(d.pop("body", UNSET))

        def _parse_category(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        category = _parse_category(d.pop("category", UNSET))

        def _parse_summary(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        summary = _parse_summary(d.pop("summary", UNSET))

        def _parse_tags(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                tags_type_0 = cast(list[Any], data)

                return tags_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        tags = _parse_tags(d.pop("tags", UNSET))

        def _parse_sources(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                sources_type_0 = cast(list[Any], data)

                return sources_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        sources = _parse_sources(d.pop("sources", UNSET))

        def _parse_metadata(data: object) -> KnowledgeUpdateRequestMetadataType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                metadata_type_0 = KnowledgeUpdateRequestMetadataType0.from_dict(data)

                return metadata_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(KnowledgeUpdateRequestMetadataType0 | None | Unset, data)

        metadata = _parse_metadata(d.pop("metadata", UNSET))

        def _parse_change_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        change_reason = _parse_change_reason(d.pop("change_reason", UNSET))

        def _parse_valid_from(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        valid_from = _parse_valid_from(d.pop("valid_from", UNSET))

        def _parse_valid_until(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        valid_until = _parse_valid_until(d.pop("valid_until", UNSET))

        def _parse_recheck_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        recheck_at = _parse_recheck_at(d.pop("recheck_at", UNSET))

        def _parse_summary_of_revision(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        summary_of_revision = _parse_summary_of_revision(d.pop("summary_of_revision", UNSET))

        global_scope = d.pop("global_scope", UNSET)

        knowledge_update_request = cls(
            identity=identity,
            idempotency_key=idempotency_key,
            project_id=project_id,
            if_revision=if_revision,
            claim_epoch=claim_epoch,
            title=title,
            body=body,
            category=category,
            summary=summary,
            tags=tags,
            sources=sources,
            metadata=metadata,
            change_reason=change_reason,
            valid_from=valid_from,
            valid_until=valid_until,
            recheck_at=recheck_at,
            summary_of_revision=summary_of_revision,
            global_scope=global_scope,
        )

        knowledge_update_request.additional_properties = d
        return knowledge_update_request

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
