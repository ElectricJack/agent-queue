from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.knowledge_import_args_operation import KnowledgeImportArgsOperation
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.inventory_root_spec import InventoryRootSpec
    from ..models.knowledge_import_args_expected_revisions import KnowledgeImportArgsExpectedRevisions
    from ..models.knowledge_import_args_expected_source_hashes import KnowledgeImportArgsExpectedSourceHashes
    from ..models.knowledge_import_args_scope_aliases_type_0 import KnowledgeImportArgsScopeAliasesType0


T = TypeVar("T", bound="KnowledgeImportArgs")


@_attrs_define
class KnowledgeImportArgs:
    """
    Attributes:
        operation (KnowledgeImportArgsOperation | Unset):  Default: KnowledgeImportArgsOperation.DRY_RUN.
        roots (list[InventoryRootSpec] | Unset):
        vector_export (None | str | Unset):
        scope_aliases (KnowledgeImportArgsScopeAliasesType0 | None | Unset):
        source_installation_id (None | str | Unset):
        snapshot_id (None | str | Unset):
        snapshot_timestamp (None | str | Unset):
        project_id (None | str | Unset):
        global_scope (bool | Unset):  Default: False.
        manifest_content_base64 (None | str | Unset):
        manifest_sha256 (None | str | Unset):
        selected_item_ids (list[str] | Unset):
        expected_revisions (KnowledgeImportArgsExpectedRevisions | Unset):
        expected_source_hashes (KnowledgeImportArgsExpectedSourceHashes | Unset):
        idempotency_key (None | str | Unset):
        backup_receipt (None | str | Unset):
        run_id (None | str | Unset):
        limit (int | Unset):  Default: 100.
    """

    operation: KnowledgeImportArgsOperation | Unset = KnowledgeImportArgsOperation.DRY_RUN
    roots: list[InventoryRootSpec] | Unset = UNSET
    vector_export: None | str | Unset = UNSET
    scope_aliases: KnowledgeImportArgsScopeAliasesType0 | None | Unset = UNSET
    source_installation_id: None | str | Unset = UNSET
    snapshot_id: None | str | Unset = UNSET
    snapshot_timestamp: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    global_scope: bool | Unset = False
    manifest_content_base64: None | str | Unset = UNSET
    manifest_sha256: None | str | Unset = UNSET
    selected_item_ids: list[str] | Unset = UNSET
    expected_revisions: KnowledgeImportArgsExpectedRevisions | Unset = UNSET
    expected_source_hashes: KnowledgeImportArgsExpectedSourceHashes | Unset = UNSET
    idempotency_key: None | str | Unset = UNSET
    backup_receipt: None | str | Unset = UNSET
    run_id: None | str | Unset = UNSET
    limit: int | Unset = 100

    def to_dict(self) -> dict[str, Any]:
        from ..models.knowledge_import_args_scope_aliases_type_0 import KnowledgeImportArgsScopeAliasesType0

        operation: str | Unset = UNSET
        if not isinstance(self.operation, Unset):
            operation = self.operation.value

        roots: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.roots, Unset):
            roots = []
            for roots_item_data in self.roots:
                roots_item = roots_item_data.to_dict()
                roots.append(roots_item)

        vector_export: None | str | Unset
        if isinstance(self.vector_export, Unset):
            vector_export = UNSET
        else:
            vector_export = self.vector_export

        scope_aliases: dict[str, Any] | None | Unset
        if isinstance(self.scope_aliases, Unset):
            scope_aliases = UNSET
        elif isinstance(self.scope_aliases, KnowledgeImportArgsScopeAliasesType0):
            scope_aliases = self.scope_aliases.to_dict()
        else:
            scope_aliases = self.scope_aliases

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

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        global_scope = self.global_scope

        manifest_content_base64: None | str | Unset
        if isinstance(self.manifest_content_base64, Unset):
            manifest_content_base64 = UNSET
        else:
            manifest_content_base64 = self.manifest_content_base64

        manifest_sha256: None | str | Unset
        if isinstance(self.manifest_sha256, Unset):
            manifest_sha256 = UNSET
        else:
            manifest_sha256 = self.manifest_sha256

        selected_item_ids: list[str] | Unset = UNSET
        if not isinstance(self.selected_item_ids, Unset):
            selected_item_ids = self.selected_item_ids

        expected_revisions: dict[str, Any] | Unset = UNSET
        if not isinstance(self.expected_revisions, Unset):
            expected_revisions = self.expected_revisions.to_dict()

        expected_source_hashes: dict[str, Any] | Unset = UNSET
        if not isinstance(self.expected_source_hashes, Unset):
            expected_source_hashes = self.expected_source_hashes.to_dict()

        idempotency_key: None | str | Unset
        if isinstance(self.idempotency_key, Unset):
            idempotency_key = UNSET
        else:
            idempotency_key = self.idempotency_key

        backup_receipt: None | str | Unset
        if isinstance(self.backup_receipt, Unset):
            backup_receipt = UNSET
        else:
            backup_receipt = self.backup_receipt

        run_id: None | str | Unset
        if isinstance(self.run_id, Unset):
            run_id = UNSET
        else:
            run_id = self.run_id

        limit = self.limit

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if operation is not UNSET:
            field_dict["operation"] = operation
        if roots is not UNSET:
            field_dict["roots"] = roots
        if vector_export is not UNSET:
            field_dict["vector_export"] = vector_export
        if scope_aliases is not UNSET:
            field_dict["scope_aliases"] = scope_aliases
        if source_installation_id is not UNSET:
            field_dict["source_installation_id"] = source_installation_id
        if snapshot_id is not UNSET:
            field_dict["snapshot_id"] = snapshot_id
        if snapshot_timestamp is not UNSET:
            field_dict["snapshot_timestamp"] = snapshot_timestamp
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if global_scope is not UNSET:
            field_dict["global_scope"] = global_scope
        if manifest_content_base64 is not UNSET:
            field_dict["manifest_content_base64"] = manifest_content_base64
        if manifest_sha256 is not UNSET:
            field_dict["manifest_sha256"] = manifest_sha256
        if selected_item_ids is not UNSET:
            field_dict["selected_item_ids"] = selected_item_ids
        if expected_revisions is not UNSET:
            field_dict["expected_revisions"] = expected_revisions
        if expected_source_hashes is not UNSET:
            field_dict["expected_source_hashes"] = expected_source_hashes
        if idempotency_key is not UNSET:
            field_dict["idempotency_key"] = idempotency_key
        if backup_receipt is not UNSET:
            field_dict["backup_receipt"] = backup_receipt
        if run_id is not UNSET:
            field_dict["run_id"] = run_id
        if limit is not UNSET:
            field_dict["limit"] = limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.inventory_root_spec import InventoryRootSpec
        from ..models.knowledge_import_args_expected_revisions import KnowledgeImportArgsExpectedRevisions
        from ..models.knowledge_import_args_expected_source_hashes import KnowledgeImportArgsExpectedSourceHashes
        from ..models.knowledge_import_args_scope_aliases_type_0 import KnowledgeImportArgsScopeAliasesType0

        d = dict(src_dict)
        _operation = d.pop("operation", UNSET)
        operation: KnowledgeImportArgsOperation | Unset
        if isinstance(_operation, Unset):
            operation = UNSET
        else:
            operation = KnowledgeImportArgsOperation(_operation)

        _roots = d.pop("roots", UNSET)
        roots: list[InventoryRootSpec] | Unset = UNSET
        if _roots is not UNSET:
            roots = []
            for roots_item_data in _roots:
                roots_item = InventoryRootSpec.from_dict(roots_item_data)

                roots.append(roots_item)

        def _parse_vector_export(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        vector_export = _parse_vector_export(d.pop("vector_export", UNSET))

        def _parse_scope_aliases(data: object) -> KnowledgeImportArgsScopeAliasesType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                scope_aliases_type_0 = KnowledgeImportArgsScopeAliasesType0.from_dict(data)

                return scope_aliases_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(KnowledgeImportArgsScopeAliasesType0 | None | Unset, data)

        scope_aliases = _parse_scope_aliases(d.pop("scope_aliases", UNSET))

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

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        global_scope = d.pop("global_scope", UNSET)

        def _parse_manifest_content_base64(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        manifest_content_base64 = _parse_manifest_content_base64(d.pop("manifest_content_base64", UNSET))

        def _parse_manifest_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        manifest_sha256 = _parse_manifest_sha256(d.pop("manifest_sha256", UNSET))

        selected_item_ids = cast(list[str], d.pop("selected_item_ids", UNSET))

        _expected_revisions = d.pop("expected_revisions", UNSET)
        expected_revisions: KnowledgeImportArgsExpectedRevisions | Unset
        if isinstance(_expected_revisions, Unset):
            expected_revisions = UNSET
        else:
            expected_revisions = KnowledgeImportArgsExpectedRevisions.from_dict(_expected_revisions)

        _expected_source_hashes = d.pop("expected_source_hashes", UNSET)
        expected_source_hashes: KnowledgeImportArgsExpectedSourceHashes | Unset
        if isinstance(_expected_source_hashes, Unset):
            expected_source_hashes = UNSET
        else:
            expected_source_hashes = KnowledgeImportArgsExpectedSourceHashes.from_dict(_expected_source_hashes)

        def _parse_idempotency_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        idempotency_key = _parse_idempotency_key(d.pop("idempotency_key", UNSET))

        def _parse_backup_receipt(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        backup_receipt = _parse_backup_receipt(d.pop("backup_receipt", UNSET))

        def _parse_run_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        run_id = _parse_run_id(d.pop("run_id", UNSET))

        limit = d.pop("limit", UNSET)

        knowledge_import_args = cls(
            operation=operation,
            roots=roots,
            vector_export=vector_export,
            scope_aliases=scope_aliases,
            source_installation_id=source_installation_id,
            snapshot_id=snapshot_id,
            snapshot_timestamp=snapshot_timestamp,
            project_id=project_id,
            global_scope=global_scope,
            manifest_content_base64=manifest_content_base64,
            manifest_sha256=manifest_sha256,
            selected_item_ids=selected_item_ids,
            expected_revisions=expected_revisions,
            expected_source_hashes=expected_source_hashes,
            idempotency_key=idempotency_key,
            backup_receipt=backup_receipt,
            run_id=run_id,
            limit=limit,
        )

        return knowledge_import_args
