from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.artifact_ref_dto import ArtifactRefDTO


T = TypeVar("T", bound="PlaybookArtifactSummaryDTO")


@_attrs_define
class PlaybookArtifactSummaryDTO:
    """One stored artifact as the activation chooser lists it.

    ``is_active`` is per *playbook*, not per scope: a playbook activated in
    more than one scope has more than one active artifact, and every one of
    them is flagged.

        Attributes:
            artifact (ArtifactRefDTO): Roadmap §4 ``ArtifactRef``, projected.  Identifies exactly one
                immutable artifact; every graph, diff and overlay response carries one.
            scope (str | Unset):  Default: 'system'.
            scope_identifier (None | str | Unset):
            size_bytes (int | Unset):  Default: 0.
            created_at (float | None | Unset):
            is_active (bool | Unset):  Default: False.
    """

    artifact: ArtifactRefDTO
    scope: str | Unset = "system"
    scope_identifier: None | str | Unset = UNSET
    size_bytes: int | Unset = 0
    created_at: float | None | Unset = UNSET
    is_active: bool | Unset = False

    def to_dict(self) -> dict[str, Any]:
        artifact = self.artifact.to_dict()

        scope = self.scope

        scope_identifier: None | str | Unset
        if isinstance(self.scope_identifier, Unset):
            scope_identifier = UNSET
        else:
            scope_identifier = self.scope_identifier

        size_bytes = self.size_bytes

        created_at: float | None | Unset
        if isinstance(self.created_at, Unset):
            created_at = UNSET
        else:
            created_at = self.created_at

        is_active = self.is_active

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "artifact": artifact,
            }
        )
        if scope is not UNSET:
            field_dict["scope"] = scope
        if scope_identifier is not UNSET:
            field_dict["scope_identifier"] = scope_identifier
        if size_bytes is not UNSET:
            field_dict["size_bytes"] = size_bytes
        if created_at is not UNSET:
            field_dict["created_at"] = created_at
        if is_active is not UNSET:
            field_dict["is_active"] = is_active

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.artifact_ref_dto import ArtifactRefDTO

        d = dict(src_dict)
        artifact = ArtifactRefDTO.from_dict(d.pop("artifact"))

        scope = d.pop("scope", UNSET)

        def _parse_scope_identifier(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        scope_identifier = _parse_scope_identifier(d.pop("scope_identifier", UNSET))

        size_bytes = d.pop("size_bytes", UNSET)

        def _parse_created_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        created_at = _parse_created_at(d.pop("created_at", UNSET))

        is_active = d.pop("is_active", UNSET)

        playbook_artifact_summary_dto = cls(
            artifact=artifact,
            scope=scope,
            scope_identifier=scope_identifier,
            size_bytes=size_bytes,
            created_at=created_at,
            is_active=is_active,
        )

        return playbook_artifact_summary_dto
