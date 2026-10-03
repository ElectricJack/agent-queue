from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

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
    """The sealed inventory and reconciliation returned by the operator dry run.

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
        outcome (str | Unset):  Default: 'read'.
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
    outcome: str | Unset = "read"
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

        outcome = self.outcome

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

        outcome = d.pop("outcome", UNSET)

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
