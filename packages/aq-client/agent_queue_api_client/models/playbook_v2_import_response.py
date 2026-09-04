from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from attrs import define as _attrs_define

from ..models.playbook_v2_import_response_scope import PlaybookV2ImportResponseScope
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.compiler_diagnostic_dto import CompilerDiagnosticDTO


T = TypeVar("T", bound="PlaybookV2ImportResponse")


@_attrs_define
class PlaybookV2ImportResponse:
    """A reviewed artifact persisted as an inactive activation candidate.

    Attributes:
        success (bool):
        playbook_id (str):
        artifact_sha256 (str):
        scope (PlaybookV2ImportResponseScope):
        schema_version (Literal[2]):
        version (int):
        source_sha256 (str):
        contract_fingerprint (str):
        reviewed_by (str):
        reviewed_at (str):
        activated (bool):
        scope_identifier (None | str | Unset):
        diagnostics (list[CompilerDiagnosticDTO] | Unset):
    """

    success: bool
    playbook_id: str
    artifact_sha256: str
    scope: PlaybookV2ImportResponseScope
    schema_version: Literal[2]
    version: int
    source_sha256: str
    contract_fingerprint: str
    reviewed_by: str
    reviewed_at: str
    activated: bool
    scope_identifier: None | str | Unset = UNSET
    diagnostics: list[CompilerDiagnosticDTO] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        playbook_id = self.playbook_id

        artifact_sha256 = self.artifact_sha256

        scope = self.scope.value

        schema_version = self.schema_version

        version = self.version

        source_sha256 = self.source_sha256

        contract_fingerprint = self.contract_fingerprint

        reviewed_by = self.reviewed_by

        reviewed_at = self.reviewed_at

        activated = self.activated

        scope_identifier: None | str | Unset
        if isinstance(self.scope_identifier, Unset):
            scope_identifier = UNSET
        else:
            scope_identifier = self.scope_identifier

        diagnostics: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.diagnostics, Unset):
            diagnostics = []
            for diagnostics_item_data in self.diagnostics:
                diagnostics_item = diagnostics_item_data.to_dict()
                diagnostics.append(diagnostics_item)

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "success": success,
                "playbook_id": playbook_id,
                "artifact_sha256": artifact_sha256,
                "scope": scope,
                "schema_version": schema_version,
                "version": version,
                "source_sha256": source_sha256,
                "contract_fingerprint": contract_fingerprint,
                "reviewed_by": reviewed_by,
                "reviewed_at": reviewed_at,
                "activated": activated,
            }
        )
        if scope_identifier is not UNSET:
            field_dict["scope_identifier"] = scope_identifier
        if diagnostics is not UNSET:
            field_dict["diagnostics"] = diagnostics

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiler_diagnostic_dto import CompilerDiagnosticDTO

        d = dict(src_dict)
        success = d.pop("success")

        playbook_id = d.pop("playbook_id")

        artifact_sha256 = d.pop("artifact_sha256")

        scope = PlaybookV2ImportResponseScope(d.pop("scope"))

        schema_version = cast(Literal[2], d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        version = d.pop("version")

        source_sha256 = d.pop("source_sha256")

        contract_fingerprint = d.pop("contract_fingerprint")

        reviewed_by = d.pop("reviewed_by")

        reviewed_at = d.pop("reviewed_at")

        activated = d.pop("activated")

        def _parse_scope_identifier(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        scope_identifier = _parse_scope_identifier(d.pop("scope_identifier", UNSET))

        _diagnostics = d.pop("diagnostics", UNSET)
        diagnostics: list[CompilerDiagnosticDTO] | Unset = UNSET
        if _diagnostics is not UNSET:
            diagnostics = []
            for diagnostics_item_data in _diagnostics:
                diagnostics_item = CompilerDiagnosticDTO.from_dict(diagnostics_item_data)

                diagnostics.append(diagnostics_item)

        playbook_v2_import_response = cls(
            success=success,
            playbook_id=playbook_id,
            artifact_sha256=artifact_sha256,
            scope=scope,
            schema_version=schema_version,
            version=version,
            source_sha256=source_sha256,
            contract_fingerprint=contract_fingerprint,
            reviewed_by=reviewed_by,
            reviewed_at=reviewed_at,
            activated=activated,
            scope_identifier=scope_identifier,
            diagnostics=diagnostics,
        )

        return playbook_v2_import_response
