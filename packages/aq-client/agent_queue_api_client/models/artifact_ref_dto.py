from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="ArtifactRefDTO")


@_attrs_define
class ArtifactRefDTO:
    """Roadmap §4 ``ArtifactRef``, projected.  Identifies exactly one
    immutable artifact; every graph, diff and overlay response carries one.

        Attributes:
            playbook_id (str):
            artifact_sha256 (str):
            schema_generation (int):
            contract_fingerprint (str):
            source_digest (str):
            compiler_build (str):
            compiled_at (None | str | Unset):
            version (int | Unset):  Default: 0.
    """

    playbook_id: str
    artifact_sha256: str
    schema_generation: int
    contract_fingerprint: str
    source_digest: str
    compiler_build: str
    compiled_at: None | str | Unset = UNSET
    version: int | Unset = 0

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        artifact_sha256 = self.artifact_sha256

        schema_generation = self.schema_generation

        contract_fingerprint = self.contract_fingerprint

        source_digest = self.source_digest

        compiler_build = self.compiler_build

        compiled_at: None | str | Unset
        if isinstance(self.compiled_at, Unset):
            compiled_at = UNSET
        else:
            compiled_at = self.compiled_at

        version = self.version

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "playbook_id": playbook_id,
                "artifact_sha256": artifact_sha256,
                "schema_generation": schema_generation,
                "contract_fingerprint": contract_fingerprint,
                "source_digest": source_digest,
                "compiler_build": compiler_build,
            }
        )
        if compiled_at is not UNSET:
            field_dict["compiled_at"] = compiled_at
        if version is not UNSET:
            field_dict["version"] = version

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        artifact_sha256 = d.pop("artifact_sha256")

        schema_generation = d.pop("schema_generation")

        contract_fingerprint = d.pop("contract_fingerprint")

        source_digest = d.pop("source_digest")

        compiler_build = d.pop("compiler_build")

        def _parse_compiled_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        compiled_at = _parse_compiled_at(d.pop("compiled_at", UNSET))

        version = d.pop("version", UNSET)

        artifact_ref_dto = cls(
            playbook_id=playbook_id,
            artifact_sha256=artifact_sha256,
            schema_generation=schema_generation,
            contract_fingerprint=contract_fingerprint,
            source_digest=source_digest,
            compiler_build=compiler_build,
            compiled_at=compiled_at,
            version=version,
        )

        return artifact_ref_dto
