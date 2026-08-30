from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CompilePlaybookResponse")


@_attrs_define
class CompilePlaybookResponse:
    """
    Attributes:
        compiled (bool | Unset):  Default: False.
        playbook_id (str | Unset):  Default: ''.
        version (int | Unset):  Default: 0.
        source_hash (str | Unset):  Default: ''.
        skipped (bool | Unset):  Default: False.
        retries_used (int | Unset):  Default: 0.
        node_count (int | None | Unset):
        triggers (list[str] | None | Unset):
        scope (None | str | Unset):
        errors (list[str] | None | Unset):
    """

    compiled: bool | Unset = False
    playbook_id: str | Unset = ""
    version: int | Unset = 0
    source_hash: str | Unset = ""
    skipped: bool | Unset = False
    retries_used: int | Unset = 0
    node_count: int | None | Unset = UNSET
    triggers: list[str] | None | Unset = UNSET
    scope: None | str | Unset = UNSET
    errors: list[str] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        compiled = self.compiled

        playbook_id = self.playbook_id

        version = self.version

        source_hash = self.source_hash

        skipped = self.skipped

        retries_used = self.retries_used

        node_count: int | None | Unset
        if isinstance(self.node_count, Unset):
            node_count = UNSET
        else:
            node_count = self.node_count

        triggers: list[str] | None | Unset
        if isinstance(self.triggers, Unset):
            triggers = UNSET
        elif isinstance(self.triggers, list):
            triggers = self.triggers

        else:
            triggers = self.triggers

        scope: None | str | Unset
        if isinstance(self.scope, Unset):
            scope = UNSET
        else:
            scope = self.scope

        errors: list[str] | None | Unset
        if isinstance(self.errors, Unset):
            errors = UNSET
        elif isinstance(self.errors, list):
            errors = self.errors

        else:
            errors = self.errors

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if compiled is not UNSET:
            field_dict["compiled"] = compiled
        if playbook_id is not UNSET:
            field_dict["playbook_id"] = playbook_id
        if version is not UNSET:
            field_dict["version"] = version
        if source_hash is not UNSET:
            field_dict["source_hash"] = source_hash
        if skipped is not UNSET:
            field_dict["skipped"] = skipped
        if retries_used is not UNSET:
            field_dict["retries_used"] = retries_used
        if node_count is not UNSET:
            field_dict["node_count"] = node_count
        if triggers is not UNSET:
            field_dict["triggers"] = triggers
        if scope is not UNSET:
            field_dict["scope"] = scope
        if errors is not UNSET:
            field_dict["errors"] = errors

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        compiled = d.pop("compiled", UNSET)

        playbook_id = d.pop("playbook_id", UNSET)

        version = d.pop("version", UNSET)

        source_hash = d.pop("source_hash", UNSET)

        skipped = d.pop("skipped", UNSET)

        retries_used = d.pop("retries_used", UNSET)

        def _parse_node_count(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        node_count = _parse_node_count(d.pop("node_count", UNSET))

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

        def _parse_scope(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        scope = _parse_scope(d.pop("scope", UNSET))

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

        compile_playbook_response = cls(
            compiled=compiled,
            playbook_id=playbook_id,
            version=version,
            source_hash=source_hash,
            skipped=skipped,
            retries_used=retries_used,
            node_count=node_count,
            triggers=triggers,
            scope=scope,
            errors=errors,
        )

        compile_playbook_response.additional_properties = d
        return compile_playbook_response

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
