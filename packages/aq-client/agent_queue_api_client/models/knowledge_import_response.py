from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.knowledge_import_response_counts_type_0 import KnowledgeImportResponseCountsType0
    from ..models.knowledge_import_response_identities_type_0_item import KnowledgeImportResponseIdentitiesType0Item
    from ..models.knowledge_import_response_items_type_0_item import KnowledgeImportResponseItemsType0Item
    from ..models.knowledge_import_response_mappings_type_0_item import KnowledgeImportResponseMappingsType0Item


T = TypeVar("T", bound="KnowledgeImportResponse")


@_attrs_define
class KnowledgeImportResponse:
    """Stable envelope for inventory and durable apply/resume projections.

    Attributes:
        success (bool | Unset):  Default: True.
        outcome (None | str | Unset):
        record_id (None | str | Unset):
        replay (bool | Unset):  Default: False.
        content_sha256 (None | str | Unset):
        revision_id (None | str | Unset):
        source_installation_id (None | str | Unset):
        snapshot_id (None | str | Unset):
        snapshot_timestamp (None | str | Unset):
        manifest_sha256 (None | str | Unset):
        manifest_content_base64 (None | str | Unset):
        vector_observation (None | str | Unset):
        counts (KnowledgeImportResponseCountsType0 | None | Unset):
        items (list[KnowledgeImportResponseItemsType0Item] | None | Unset):
        mappings (list[KnowledgeImportResponseMappingsType0Item] | None | Unset):
        identities (list[KnowledgeImportResponseIdentitiesType0Item] | None | Unset):
        run_id (None | str | Unset):
        state (None | str | Unset):
    """

    success: bool | Unset = True
    outcome: None | str | Unset = UNSET
    record_id: None | str | Unset = UNSET
    replay: bool | Unset = False
    content_sha256: None | str | Unset = UNSET
    revision_id: None | str | Unset = UNSET
    source_installation_id: None | str | Unset = UNSET
    snapshot_id: None | str | Unset = UNSET
    snapshot_timestamp: None | str | Unset = UNSET
    manifest_sha256: None | str | Unset = UNSET
    manifest_content_base64: None | str | Unset = UNSET
    vector_observation: None | str | Unset = UNSET
    counts: KnowledgeImportResponseCountsType0 | None | Unset = UNSET
    items: list[KnowledgeImportResponseItemsType0Item] | None | Unset = UNSET
    mappings: list[KnowledgeImportResponseMappingsType0Item] | None | Unset = UNSET
    identities: list[KnowledgeImportResponseIdentitiesType0Item] | None | Unset = UNSET
    run_id: None | str | Unset = UNSET
    state: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.knowledge_import_response_counts_type_0 import KnowledgeImportResponseCountsType0

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

        source_installation_id: None | str | Unset
        if isinstance(self.source_installation_id, Unset):
            source_installation_id = UNSET
        else:
            source_installation_id = self.source_installation_id

        snapshot_id: None | str | Unset
        if isinstance(self.snapshot_id, Unset):
            snapshot_id = UNSET
        else:
            snapshot_id = self.snapshot_id

        snapshot_timestamp: None | str | Unset
        if isinstance(self.snapshot_timestamp, Unset):
            snapshot_timestamp = UNSET
        else:
            snapshot_timestamp = self.snapshot_timestamp

        manifest_sha256: None | str | Unset
        if isinstance(self.manifest_sha256, Unset):
            manifest_sha256 = UNSET
        else:
            manifest_sha256 = self.manifest_sha256

        manifest_content_base64: None | str | Unset
        if isinstance(self.manifest_content_base64, Unset):
            manifest_content_base64 = UNSET
        else:
            manifest_content_base64 = self.manifest_content_base64

        vector_observation: None | str | Unset
        if isinstance(self.vector_observation, Unset):
            vector_observation = UNSET
        else:
            vector_observation = self.vector_observation

        counts: dict[str, Any] | None | Unset
        if isinstance(self.counts, Unset):
            counts = UNSET
        elif isinstance(self.counts, KnowledgeImportResponseCountsType0):
            counts = self.counts.to_dict()
        else:
            counts = self.counts

        items: list[dict[str, Any]] | None | Unset
        if isinstance(self.items, Unset):
            items = UNSET
        elif isinstance(self.items, list):
            items = []
            for items_type_0_item_data in self.items:
                items_type_0_item = items_type_0_item_data.to_dict()
                items.append(items_type_0_item)

        else:
            items = self.items

        mappings: list[dict[str, Any]] | None | Unset
        if isinstance(self.mappings, Unset):
            mappings = UNSET
        elif isinstance(self.mappings, list):
            mappings = []
            for mappings_type_0_item_data in self.mappings:
                mappings_type_0_item = mappings_type_0_item_data.to_dict()
                mappings.append(mappings_type_0_item)

        else:
            mappings = self.mappings

        identities: list[dict[str, Any]] | None | Unset
        if isinstance(self.identities, Unset):
            identities = UNSET
        elif isinstance(self.identities, list):
            identities = []
            for identities_type_0_item_data in self.identities:
                identities_type_0_item = identities_type_0_item_data.to_dict()
                identities.append(identities_type_0_item)

        else:
            identities = self.identities

        run_id: None | str | Unset
        if isinstance(self.run_id, Unset):
            run_id = UNSET
        else:
            run_id = self.run_id

        state: None | str | Unset
        if isinstance(self.state, Unset):
            state = UNSET
        else:
            state = self.state

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
        if source_installation_id is not UNSET:
            field_dict["source_installation_id"] = source_installation_id
        if snapshot_id is not UNSET:
            field_dict["snapshot_id"] = snapshot_id
        if snapshot_timestamp is not UNSET:
            field_dict["snapshot_timestamp"] = snapshot_timestamp
        if manifest_sha256 is not UNSET:
            field_dict["manifest_sha256"] = manifest_sha256
        if manifest_content_base64 is not UNSET:
            field_dict["manifest_content_base64"] = manifest_content_base64
        if vector_observation is not UNSET:
            field_dict["vector_observation"] = vector_observation
        if counts is not UNSET:
            field_dict["counts"] = counts
        if items is not UNSET:
            field_dict["items"] = items
        if mappings is not UNSET:
            field_dict["mappings"] = mappings
        if identities is not UNSET:
            field_dict["identities"] = identities
        if run_id is not UNSET:
            field_dict["run_id"] = run_id
        if state is not UNSET:
            field_dict["state"] = state

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.knowledge_import_response_counts_type_0 import KnowledgeImportResponseCountsType0
        from ..models.knowledge_import_response_identities_type_0_item import KnowledgeImportResponseIdentitiesType0Item
        from ..models.knowledge_import_response_items_type_0_item import KnowledgeImportResponseItemsType0Item
        from ..models.knowledge_import_response_mappings_type_0_item import KnowledgeImportResponseMappingsType0Item

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

        def _parse_source_installation_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source_installation_id = _parse_source_installation_id(d.pop("source_installation_id", UNSET))

        def _parse_snapshot_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        snapshot_id = _parse_snapshot_id(d.pop("snapshot_id", UNSET))

        def _parse_snapshot_timestamp(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        snapshot_timestamp = _parse_snapshot_timestamp(d.pop("snapshot_timestamp", UNSET))

        def _parse_manifest_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        manifest_sha256 = _parse_manifest_sha256(d.pop("manifest_sha256", UNSET))

        def _parse_manifest_content_base64(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        manifest_content_base64 = _parse_manifest_content_base64(d.pop("manifest_content_base64", UNSET))

        def _parse_vector_observation(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        vector_observation = _parse_vector_observation(d.pop("vector_observation", UNSET))

        def _parse_counts(data: object) -> KnowledgeImportResponseCountsType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                counts_type_0 = KnowledgeImportResponseCountsType0.from_dict(data)

                return counts_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(KnowledgeImportResponseCountsType0 | None | Unset, data)

        counts = _parse_counts(d.pop("counts", UNSET))

        def _parse_items(data: object) -> list[KnowledgeImportResponseItemsType0Item] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                items_type_0 = []
                _items_type_0 = data
                for items_type_0_item_data in _items_type_0:
                    items_type_0_item = KnowledgeImportResponseItemsType0Item.from_dict(items_type_0_item_data)

                    items_type_0.append(items_type_0_item)

                return items_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[KnowledgeImportResponseItemsType0Item] | None | Unset, data)

        items = _parse_items(d.pop("items", UNSET))

        def _parse_mappings(data: object) -> list[KnowledgeImportResponseMappingsType0Item] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                mappings_type_0 = []
                _mappings_type_0 = data
                for mappings_type_0_item_data in _mappings_type_0:
                    mappings_type_0_item = KnowledgeImportResponseMappingsType0Item.from_dict(mappings_type_0_item_data)

                    mappings_type_0.append(mappings_type_0_item)

                return mappings_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[KnowledgeImportResponseMappingsType0Item] | None | Unset, data)

        mappings = _parse_mappings(d.pop("mappings", UNSET))

        def _parse_identities(data: object) -> list[KnowledgeImportResponseIdentitiesType0Item] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                identities_type_0 = []
                _identities_type_0 = data
                for identities_type_0_item_data in _identities_type_0:
                    identities_type_0_item = KnowledgeImportResponseIdentitiesType0Item.from_dict(
                        identities_type_0_item_data
                    )

                    identities_type_0.append(identities_type_0_item)

                return identities_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[KnowledgeImportResponseIdentitiesType0Item] | None | Unset, data)

        identities = _parse_identities(d.pop("identities", UNSET))

        def _parse_run_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        run_id = _parse_run_id(d.pop("run_id", UNSET))

        def _parse_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        state = _parse_state(d.pop("state", UNSET))

        knowledge_import_response = cls(
            success=success,
            outcome=outcome,
            record_id=record_id,
            replay=replay,
            content_sha256=content_sha256,
            revision_id=revision_id,
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
            run_id=run_id,
            state=state,
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
