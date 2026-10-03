from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.knowledge_create_request_metadata_type_0 import KnowledgeCreateRequestMetadataType0


T = TypeVar("T", bound="KnowledgeCreateRequest")


@_attrs_define
class KnowledgeCreateRequest:
    """
    Attributes:
        title (str):
        body (str):
        category (str):
        idempotency_key (str):
        project_id (None | str | Unset):
        summary (None | str | Unset):
        tags (list[Any] | None | Unset):
        sources (list[Any] | None | Unset):
        metadata (KnowledgeCreateRequestMetadataType0 | None | Unset):
        claim_epoch (int | None | Unset):
        global_scope (bool | Unset):  Default: False.
        source_task_id (None | str | Unset):
        if_link_token (None | str | Unset):
    """

    title: str
    body: str
    category: str
    idempotency_key: str
    project_id: None | str | Unset = UNSET
    summary: None | str | Unset = UNSET
    tags: list[Any] | None | Unset = UNSET
    sources: list[Any] | None | Unset = UNSET
    metadata: KnowledgeCreateRequestMetadataType0 | None | Unset = UNSET
    claim_epoch: int | None | Unset = UNSET
    global_scope: bool | Unset = False
    source_task_id: None | str | Unset = UNSET
    if_link_token: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.knowledge_create_request_metadata_type_0 import KnowledgeCreateRequestMetadataType0

        title = self.title

        body = self.body

        category = self.category

        idempotency_key = self.idempotency_key

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

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
        elif isinstance(self.metadata, KnowledgeCreateRequestMetadataType0):
            metadata = self.metadata.to_dict()
        else:
            metadata = self.metadata

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        global_scope = self.global_scope

        source_task_id: None | str | Unset
        if isinstance(self.source_task_id, Unset):
            source_task_id = UNSET
        else:
            source_task_id = self.source_task_id

        if_link_token: None | str | Unset
        if isinstance(self.if_link_token, Unset):
            if_link_token = UNSET
        else:
            if_link_token = self.if_link_token

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "title": title,
                "body": body,
                "category": category,
                "idempotency_key": idempotency_key,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if summary is not UNSET:
            field_dict["summary"] = summary
        if tags is not UNSET:
            field_dict["tags"] = tags
        if sources is not UNSET:
            field_dict["sources"] = sources
        if metadata is not UNSET:
            field_dict["metadata"] = metadata
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch
        if global_scope is not UNSET:
            field_dict["global_scope"] = global_scope
        if source_task_id is not UNSET:
            field_dict["source_task_id"] = source_task_id
        if if_link_token is not UNSET:
            field_dict["if_link_token"] = if_link_token

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.knowledge_create_request_metadata_type_0 import KnowledgeCreateRequestMetadataType0

        d = dict(src_dict)
        title = d.pop("title")

        body = d.pop("body")

        category = d.pop("category")

        idempotency_key = d.pop("idempotency_key")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

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

        def _parse_metadata(data: object) -> KnowledgeCreateRequestMetadataType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                metadata_type_0 = KnowledgeCreateRequestMetadataType0.from_dict(data)

                return metadata_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(KnowledgeCreateRequestMetadataType0 | None | Unset, data)

        metadata = _parse_metadata(d.pop("metadata", UNSET))

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        global_scope = d.pop("global_scope", UNSET)

        def _parse_source_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source_task_id = _parse_source_task_id(d.pop("source_task_id", UNSET))

        def _parse_if_link_token(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        if_link_token = _parse_if_link_token(d.pop("if_link_token", UNSET))

        knowledge_create_request = cls(
            title=title,
            body=body,
            category=category,
            idempotency_key=idempotency_key,
            project_id=project_id,
            summary=summary,
            tags=tags,
            sources=sources,
            metadata=metadata,
            claim_epoch=claim_epoch,
            global_scope=global_scope,
            source_task_id=source_task_id,
            if_link_token=if_link_token,
        )

        knowledge_create_request.additional_properties = d
        return knowledge_create_request

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
