from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="UpdatePlaybookSourceResponse")


@_attrs_define
class UpdatePlaybookSourceResponse:
    """
    Attributes:
        playbook_id (str):
        source_hash (str):
        compiled (bool | Unset):  Default: False.
        version (int | None | Unset):
        node_count (int | None | Unset):
        scope (None | str | Unset):
        triggers (list[str] | None | Unset):
        errors (list[str] | None | Unset):
        retries_used (int | None | Unset):
        error (None | str | Unset):
        reason (None | str | Unset):
        current_source_hash (None | str | Unset):
        expected_source_hash (None | str | Unset):
    """

    playbook_id: str
    source_hash: str
    compiled: bool | Unset = False
    version: int | None | Unset = UNSET
    node_count: int | None | Unset = UNSET
    scope: None | str | Unset = UNSET
    triggers: list[str] | None | Unset = UNSET
    errors: list[str] | None | Unset = UNSET
    retries_used: int | None | Unset = UNSET
    error: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET
    current_source_hash: None | str | Unset = UNSET
    expected_source_hash: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        source_hash = self.source_hash

        compiled = self.compiled

        version: int | None | Unset
        if isinstance(self.version, Unset):
            version = UNSET
        else:
            version = self.version

        node_count: int | None | Unset
        if isinstance(self.node_count, Unset):
            node_count = UNSET
        else:
            node_count = self.node_count

        scope: None | str | Unset
        if isinstance(self.scope, Unset):
            scope = UNSET
        else:
            scope = self.scope

        triggers: list[str] | None | Unset
        if isinstance(self.triggers, Unset):
            triggers = UNSET
        elif isinstance(self.triggers, list):
            triggers = self.triggers

        else:
            triggers = self.triggers

        errors: list[str] | None | Unset
        if isinstance(self.errors, Unset):
            errors = UNSET
        elif isinstance(self.errors, list):
            errors = self.errors

        else:
            errors = self.errors

        retries_used: int | None | Unset
        if isinstance(self.retries_used, Unset):
            retries_used = UNSET
        else:
            retries_used = self.retries_used

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        current_source_hash: None | str | Unset
        if isinstance(self.current_source_hash, Unset):
            current_source_hash = UNSET
        else:
            current_source_hash = self.current_source_hash

        expected_source_hash: None | str | Unset
        if isinstance(self.expected_source_hash, Unset):
            expected_source_hash = UNSET
        else:
            expected_source_hash = self.expected_source_hash

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "playbook_id": playbook_id,
                "source_hash": source_hash,
            }
        )
        if compiled is not UNSET:
            field_dict["compiled"] = compiled
        if version is not UNSET:
            field_dict["version"] = version
        if node_count is not UNSET:
            field_dict["node_count"] = node_count
        if scope is not UNSET:
            field_dict["scope"] = scope
        if triggers is not UNSET:
            field_dict["triggers"] = triggers
        if errors is not UNSET:
            field_dict["errors"] = errors
        if retries_used is not UNSET:
            field_dict["retries_used"] = retries_used
        if error is not UNSET:
            field_dict["error"] = error
        if reason is not UNSET:
            field_dict["reason"] = reason
        if current_source_hash is not UNSET:
            field_dict["current_source_hash"] = current_source_hash
        if expected_source_hash is not UNSET:
            field_dict["expected_source_hash"] = expected_source_hash

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        source_hash = d.pop("source_hash")

        compiled = d.pop("compiled", UNSET)

        def _parse_version(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        version = _parse_version(d.pop("version", UNSET))

        def _parse_node_count(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        node_count = _parse_node_count(d.pop("node_count", UNSET))

        def _parse_scope(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        scope = _parse_scope(d.pop("scope", UNSET))

        def _parse_triggers(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                triggers_type_0 = cast(list[str], data)

                return triggers_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        triggers = _parse_triggers(d.pop("triggers", UNSET))

        def _parse_errors(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                errors_type_0 = cast(list[str], data)

                return errors_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        errors = _parse_errors(d.pop("errors", UNSET))

        def _parse_retries_used(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        retries_used = _parse_retries_used(d.pop("retries_used", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        def _parse_current_source_hash(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_source_hash = _parse_current_source_hash(d.pop("current_source_hash", UNSET))

        def _parse_expected_source_hash(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        expected_source_hash = _parse_expected_source_hash(d.pop("expected_source_hash", UNSET))

        update_playbook_source_response = cls(
            playbook_id=playbook_id,
            source_hash=source_hash,
            compiled=compiled,
            version=version,
            node_count=node_count,
            scope=scope,
            triggers=triggers,
            errors=errors,
            retries_used=retries_used,
            error=error,
            reason=reason,
            current_source_hash=current_source_hash,
            expected_source_hash=expected_source_hash,
        )

        update_playbook_source_response.additional_properties = d
        return update_playbook_source_response

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
