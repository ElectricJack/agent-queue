from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.knowledge_import_response_counts import KnowledgeImportResponseCounts
    from ..models.knowledge_import_response_identities_item import KnowledgeImportResponseIdentitiesItem
    from ..models.knowledge_import_response_items_item import KnowledgeImportResponseItemsItem
    from ..models.knowledge_import_response_mappings_item import KnowledgeImportResponseMappingsItem


T = TypeVar("T", bound="KnowledgeImportResponse")


@_attrs_define
class KnowledgeImportResponse:
    """The sealed, read-only inventory report returned by the local operator scan.

    Attributes:
        source_installation_id (str):
        snapshot_id (str):
        snapshot_timestamp (str):
        manifest_sha256 (str):
        manifest_content_base64 (str):
        vector_observation (str):
        counts (KnowledgeImportResponseCounts):
        items (list[KnowledgeImportResponseItemsItem]):
        mappings (list[KnowledgeImportResponseMappingsItem]):
        identities (list[KnowledgeImportResponseIdentitiesItem]):
        success (bool | Unset):  Default: True.
        outcome (None | str | Unset):
        record_id (None | str | Unset):
        replay (bool | Unset):  Default: False.
        content_sha256 (None | str | Unset):
        revision_id (None | str | Unset):
    """

    source_installation_id: str
    snapshot_id: str
    snapshot_timestamp: str
    manifest_sha256: str
    manifest_content_base64: str
    vector_observation: str
    counts: KnowledgeImportResponseCounts
    items: list[KnowledgeImportResponseItemsItem]
    mappings: list[KnowledgeImportResponseMappingsItem]
    identities: list[KnowledgeImportResponseIdentitiesItem]
    success: bool | Unset = True
    outcome: None | str | Unset = UNSET
    record_id: None | str | Unset = UNSET
    replay: bool | Unset = False
    content_sha256: None | str | Unset = UNSET
    revision_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        source_installation_id = self.source_installation_id

        snapshot_id = self.snapshot_id

        snapshot_timestamp = self.snapshot_timestamp

        manifest_sha256 = self.manifest_sha256

        manifest_content_base64 = self.manifest_content_base64

        vector_observation = self.vector_observation

        counts = self.counts.to_dict()

        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)

        mappings = []
        for mappings_item_data in self.mappings:
            mappings_item = mappings_item_data.to_dict()
            mappings.append(mappings_item)

        identities = []
        for identities_item_data in self.identities:
            identities_item = identities_item_data.to_dict()
            identities.append(identities_item)

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

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "source_installation_id": source_installation_id,
                "snapshot_id": snapshot_id,
                "snapshot_timestamp": snapshot_timestamp,
                "manifest_sha256": manifest_sha256,
                "manifest_content_base64": manifest_content_base64,
                "vector_observation": vector_observation,
                "counts": counts,
                "items": items,
                "mappings": mappings,
                "identities": identities,
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

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.knowledge_import_response_counts import KnowledgeImportResponseCounts
        from ..models.knowledge_import_response_identities_item import KnowledgeImportResponseIdentitiesItem
        from ..models.knowledge_import_response_items_item import KnowledgeImportResponseItemsItem
        from ..models.knowledge_import_response_mappings_item import KnowledgeImportResponseMappingsItem

        d = dict(src_dict)
        source_installation_id = d.pop("source_installation_id")

        snapshot_id = d.pop("snapshot_id")

        snapshot_timestamp = d.pop("snapshot_timestamp")

        manifest_sha256 = d.pop("manifest_sha256")

        manifest_content_base64 = d.pop("manifest_content_base64")

        vector_observation = d.pop("vector_observation")

        counts = KnowledgeImportResponseCounts.from_dict(d.pop("counts"))

        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = KnowledgeImportResponseItemsItem.from_dict(items_item_data)

            items.append(items_item)

        mappings = []
        _mappings = d.pop("mappings")
        for mappings_item_data in _mappings:
            mappings_item = KnowledgeImportResponseMappingsItem.from_dict(mappings_item_data)

            mappings.append(mappings_item)

        identities = []
        _identities = d.pop("identities")
        for identities_item_data in _identities:
            identities_item = KnowledgeImportResponseIdentitiesItem.from_dict(identities_item_data)

            identities.append(identities_item)

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

        knowledge_import_response = cls(
            source_installation_id=source_installation_id,
            snapshot_id=snapshot_id,
            snapshot_timestamp=snapshot_timestamp,
            manifest_sha256=manifest_sha256,
            manifest_content_base64=manifest_content_base64,
            vector_observation=vector_observation,
            counts=counts,
            items=items,
            mappings=mappings,
            identities=identities,
            success=success,
            outcome=outcome,
            record_id=record_id,
            replay=replay,
            content_sha256=content_sha256,
            revision_id=revision_id,
        )

        knowledge_import_response.additional_properties = d
        return knowledge_import_response

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
