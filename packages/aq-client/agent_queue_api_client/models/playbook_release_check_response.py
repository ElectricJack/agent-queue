from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_release_check_response_evidence_errors_item import (
        PlaybookReleaseCheckResponseEvidenceErrorsItem,
    )
    from ..models.playbook_release_check_response_unverified_item import PlaybookReleaseCheckResponseUnverifiedItem
    from ..models.stale_artifact_dto import StaleArtifactDTO


T = TypeVar("T", bound="PlaybookReleaseCheckResponse")


@_attrs_define
class PlaybookReleaseCheckResponse:
    """Whether every reviewed artifact still matches the live command surface.

    Attributes:
        success (bool):
        checked (list[str] | Unset):
        registry_fingerprint (None | str | Unset):
        stale (list[StaleArtifactDTO] | Unset):
        unverified (list[PlaybookReleaseCheckResponseUnverifiedItem] | Unset):
        evidence_errors (list[PlaybookReleaseCheckResponseEvidenceErrorsItem] | Unset):
        blocking_reasons (list[str] | Unset):
        error (None | str | Unset):
    """

    success: bool
    checked: list[str] | Unset = UNSET
    registry_fingerprint: None | str | Unset = UNSET
    stale: list[StaleArtifactDTO] | Unset = UNSET
    unverified: list[PlaybookReleaseCheckResponseUnverifiedItem] | Unset = UNSET
    evidence_errors: list[PlaybookReleaseCheckResponseEvidenceErrorsItem] | Unset = UNSET
    blocking_reasons: list[str] | Unset = UNSET
    error: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        checked: list[str] | Unset = UNSET
        if not isinstance(self.checked, Unset):
            checked = self.checked

        registry_fingerprint: None | str | Unset
        if isinstance(self.registry_fingerprint, Unset):
            registry_fingerprint = UNSET
        else:
            registry_fingerprint = self.registry_fingerprint

        stale: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.stale, Unset):
            stale = []
            for stale_item_data in self.stale:
                stale_item = stale_item_data.to_dict()
                stale.append(stale_item)

        unverified: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.unverified, Unset):
            unverified = []
            for unverified_item_data in self.unverified:
                unverified_item = unverified_item_data.to_dict()
                unverified.append(unverified_item)

        evidence_errors: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.evidence_errors, Unset):
            evidence_errors = []
            for evidence_errors_item_data in self.evidence_errors:
                evidence_errors_item = evidence_errors_item_data.to_dict()
                evidence_errors.append(evidence_errors_item)

        blocking_reasons: list[str] | Unset = UNSET
        if not isinstance(self.blocking_reasons, Unset):
            blocking_reasons = self.blocking_reasons

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "success": success,
            }
        )
        if checked is not UNSET:
            field_dict["checked"] = checked
        if registry_fingerprint is not UNSET:
            field_dict["registry_fingerprint"] = registry_fingerprint
        if stale is not UNSET:
            field_dict["stale"] = stale
        if unverified is not UNSET:
            field_dict["unverified"] = unverified
        if evidence_errors is not UNSET:
            field_dict["evidence_errors"] = evidence_errors
        if blocking_reasons is not UNSET:
            field_dict["blocking_reasons"] = blocking_reasons
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_release_check_response_evidence_errors_item import (
            PlaybookReleaseCheckResponseEvidenceErrorsItem,
        )
        from ..models.playbook_release_check_response_unverified_item import PlaybookReleaseCheckResponseUnverifiedItem
        from ..models.stale_artifact_dto import StaleArtifactDTO

        d = dict(src_dict)
        success = d.pop("success")

        checked = cast(list[str], d.pop("checked", UNSET))

        def _parse_registry_fingerprint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        registry_fingerprint = _parse_registry_fingerprint(d.pop("registry_fingerprint", UNSET))

        _stale = d.pop("stale", UNSET)
        stale: list[StaleArtifactDTO] | Unset = UNSET
        if _stale is not UNSET:
            stale = []
            for stale_item_data in _stale:
                stale_item = StaleArtifactDTO.from_dict(stale_item_data)

                stale.append(stale_item)

        _unverified = d.pop("unverified", UNSET)
        unverified: list[PlaybookReleaseCheckResponseUnverifiedItem] | Unset = UNSET
        if _unverified is not UNSET:
            unverified = []
            for unverified_item_data in _unverified:
                unverified_item = PlaybookReleaseCheckResponseUnverifiedItem.from_dict(unverified_item_data)

                unverified.append(unverified_item)

        _evidence_errors = d.pop("evidence_errors", UNSET)
        evidence_errors: list[PlaybookReleaseCheckResponseEvidenceErrorsItem] | Unset = UNSET
        if _evidence_errors is not UNSET:
            evidence_errors = []
            for evidence_errors_item_data in _evidence_errors:
                evidence_errors_item = PlaybookReleaseCheckResponseEvidenceErrorsItem.from_dict(
                    evidence_errors_item_data
                )

                evidence_errors.append(evidence_errors_item)

        blocking_reasons = cast(list[str], d.pop("blocking_reasons", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        playbook_release_check_response = cls(
            success=success,
            checked=checked,
            registry_fingerprint=registry_fingerprint,
            stale=stale,
            unverified=unverified,
            evidence_errors=evidence_errors,
            blocking_reasons=blocking_reasons,
            error=error,
        )

        return playbook_release_check_response
