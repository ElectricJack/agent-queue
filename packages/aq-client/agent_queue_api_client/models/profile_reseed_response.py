from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.profile_reseed_response_added_type_0 import ProfileReseedResponseAddedType0


T = TypeVar("T", bound="ProfileReseedResponse")


@_attrs_define
class ProfileReseedResponse:
    """
    Attributes:
        profile_id (str):
        path (str | Unset):  Default: ''.
        backup_path (None | str | Unset):
        created (bool | Unset):  Default: False.
        unretired (bool | Unset):  Default: False.
        mode (None | str | Unset):
        added (None | ProfileReseedResponseAddedType0 | Unset):
        changed (bool | None | Unset):
        warnings (list[str] | None | Unset):
        sync_errors (list[str] | None | Unset):
    """

    profile_id: str
    path: str | Unset = ""
    backup_path: None | str | Unset = UNSET
    created: bool | Unset = False
    unretired: bool | Unset = False
    mode: None | str | Unset = UNSET
    added: None | ProfileReseedResponseAddedType0 | Unset = UNSET
    changed: bool | None | Unset = UNSET
    warnings: list[str] | None | Unset = UNSET
    sync_errors: list[str] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.profile_reseed_response_added_type_0 import ProfileReseedResponseAddedType0

        profile_id = self.profile_id

        path = self.path

        backup_path: None | str | Unset
        if isinstance(self.backup_path, Unset):
            backup_path = UNSET
        else:
            backup_path = self.backup_path

        created = self.created

        unretired = self.unretired

        mode: None | str | Unset
        if isinstance(self.mode, Unset):
            mode = UNSET
        else:
            mode = self.mode

        added: dict[str, Any] | None | Unset
        if isinstance(self.added, Unset):
            added = UNSET
        elif isinstance(self.added, ProfileReseedResponseAddedType0):
            added = self.added.to_dict()
        else:
            added = self.added

        changed: bool | None | Unset
        if isinstance(self.changed, Unset):
            changed = UNSET
        else:
            changed = self.changed

        warnings: list[str] | None | Unset
        if isinstance(self.warnings, Unset):
            warnings = UNSET
        elif isinstance(self.warnings, list):
            warnings = self.warnings

        else:
            warnings = self.warnings

        sync_errors: list[str] | None | Unset
        if isinstance(self.sync_errors, Unset):
            sync_errors = UNSET
        elif isinstance(self.sync_errors, list):
            sync_errors = self.sync_errors

        else:
            sync_errors = self.sync_errors

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "profile_id": profile_id,
            }
        )
        if path is not UNSET:
            field_dict["path"] = path
        if backup_path is not UNSET:
            field_dict["backup_path"] = backup_path
        if created is not UNSET:
            field_dict["created"] = created
        if unretired is not UNSET:
            field_dict["unretired"] = unretired
        if mode is not UNSET:
            field_dict["mode"] = mode
        if added is not UNSET:
            field_dict["added"] = added
        if changed is not UNSET:
            field_dict["changed"] = changed
        if warnings is not UNSET:
            field_dict["warnings"] = warnings
        if sync_errors is not UNSET:
            field_dict["sync_errors"] = sync_errors

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.profile_reseed_response_added_type_0 import ProfileReseedResponseAddedType0

        d = dict(src_dict)
        profile_id = d.pop("profile_id")

        path = d.pop("path", UNSET)

        def _parse_backup_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        backup_path = _parse_backup_path(d.pop("backup_path", UNSET))

        created = d.pop("created", UNSET)

        unretired = d.pop("unretired", UNSET)

        def _parse_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        mode = _parse_mode(d.pop("mode", UNSET))

        def _parse_added(data: object) -> None | ProfileReseedResponseAddedType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                added_type_0 = ProfileReseedResponseAddedType0.from_dict(data)

                return added_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProfileReseedResponseAddedType0 | Unset, data)

        added = _parse_added(d.pop("added", UNSET))

        def _parse_changed(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        changed = _parse_changed(d.pop("changed", UNSET))

        def _parse_warnings(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                warnings_type_0 = cast(list[str], data)

                return warnings_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        warnings = _parse_warnings(d.pop("warnings", UNSET))

        def _parse_sync_errors(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                sync_errors_type_0 = cast(list[str], data)

                return sync_errors_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        sync_errors = _parse_sync_errors(d.pop("sync_errors", UNSET))

        profile_reseed_response = cls(
            profile_id=profile_id,
            path=path,
            backup_path=backup_path,
            created=created,
            unretired=unretired,
            mode=mode,
            added=added,
            changed=changed,
            warnings=warnings,
            sync_errors=sync_errors,
        )

        profile_reseed_response.additional_properties = d
        return profile_reseed_response

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
