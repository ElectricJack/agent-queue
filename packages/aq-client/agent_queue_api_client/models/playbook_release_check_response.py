from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
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
        error (None | str | Unset):
    """

    success: bool
    checked: list[str] | Unset = UNSET
    registry_fingerprint: None | str | Unset = UNSET
    stale: list[StaleArtifactDTO] | Unset = UNSET
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
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
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
            error=error,
        )

        return playbook_release_check_response
