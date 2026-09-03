from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.stale_artifact_dto_change import StaleArtifactDTOChange
from ..models.stale_artifact_dto_kind import StaleArtifactDTOKind
from ..models.stale_artifact_dto_origin import StaleArtifactDTOOrigin
from ..types import UNSET, Unset

T = TypeVar("T", bound="StaleArtifactDTO")


@_attrs_define
class StaleArtifactDTO:
    """One reviewed artifact whose compiled-against surface has moved.

    Attributes:
        playbook_id (str):
        origin (StaleArtifactDTOOrigin):
        kind (StaleArtifactDTOKind):
        dependency (str):
        change (StaleArtifactDTOChange):
        message (str):
        reviewed_fingerprint (None | str | Unset):
        current_fingerprint (None | str | Unset):
    """

    playbook_id: str
    origin: StaleArtifactDTOOrigin
    kind: StaleArtifactDTOKind
    dependency: str
    change: StaleArtifactDTOChange
    message: str
    reviewed_fingerprint: None | str | Unset = UNSET
    current_fingerprint: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        origin = self.origin.value

        kind = self.kind.value

        dependency = self.dependency

        change = self.change.value

        message = self.message

        reviewed_fingerprint: None | str | Unset
        if isinstance(self.reviewed_fingerprint, Unset):
            reviewed_fingerprint = UNSET
        else:
            reviewed_fingerprint = self.reviewed_fingerprint

        current_fingerprint: None | str | Unset
        if isinstance(self.current_fingerprint, Unset):
            current_fingerprint = UNSET
        else:
            current_fingerprint = self.current_fingerprint

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "playbook_id": playbook_id,
                "origin": origin,
                "kind": kind,
                "dependency": dependency,
                "change": change,
                "message": message,
            }
        )
        if reviewed_fingerprint is not UNSET:
            field_dict["reviewed_fingerprint"] = reviewed_fingerprint
        if current_fingerprint is not UNSET:
            field_dict["current_fingerprint"] = current_fingerprint

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        origin = StaleArtifactDTOOrigin(d.pop("origin"))

        kind = StaleArtifactDTOKind(d.pop("kind"))

        dependency = d.pop("dependency")

        change = StaleArtifactDTOChange(d.pop("change"))

        message = d.pop("message")

        def _parse_reviewed_fingerprint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reviewed_fingerprint = _parse_reviewed_fingerprint(d.pop("reviewed_fingerprint", UNSET))

        def _parse_current_fingerprint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_fingerprint = _parse_current_fingerprint(d.pop("current_fingerprint", UNSET))

        stale_artifact_dto = cls(
            playbook_id=playbook_id,
            origin=origin,
            kind=kind,
            dependency=dependency,
            change=change,
            message=message,
            reviewed_fingerprint=reviewed_fingerprint,
            current_fingerprint=current_fingerprint,
        )

        return stale_artifact_dto
