from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_last_run import PlaybookLastRun


T = TypeVar("T", bound="PlaybookSummary")


@_attrs_define
class PlaybookSummary:
    """
    Attributes:
        id (str):
        scope (str):
        triggers (list[str] | Unset):
        version (int | Unset):  Default: 0.
        compiled_at (None | str | Unset):
        node_count (int | Unset):  Default: 0.
        status (str | Unset):  Default: 'active'.
        running_count (int | Unset):  Default: 0.
        scope_identifier (None | str | Unset):
        agent_type (None | str | Unset):
        cooldown_seconds (int | None | Unset):
        cooldown_remaining (float | None | Unset):
        max_tokens (int | None | Unset):
        enabled (bool | Unset):  Default: True.
        last_run (None | PlaybookLastRun | Unset):
    """

    id: str
    scope: str
    triggers: list[str] | Unset = UNSET
    version: int | Unset = 0
    compiled_at: None | str | Unset = UNSET
    node_count: int | Unset = 0
    status: str | Unset = "active"
    running_count: int | Unset = 0
    scope_identifier: None | str | Unset = UNSET
    agent_type: None | str | Unset = UNSET
    cooldown_seconds: int | None | Unset = UNSET
    cooldown_remaining: float | None | Unset = UNSET
    max_tokens: int | None | Unset = UNSET
    enabled: bool | Unset = True
    last_run: None | PlaybookLastRun | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.playbook_last_run import PlaybookLastRun  # noqa: PLC0415

        id = self.id

        scope = self.scope

        triggers: list[str] | Unset = UNSET
        if not isinstance(self.triggers, Unset):
            triggers = self.triggers

        version = self.version

        compiled_at: None | str | Unset
        if isinstance(self.compiled_at, Unset):
            compiled_at = UNSET
        else:
            compiled_at = self.compiled_at

        node_count = self.node_count

        status = self.status

        running_count = self.running_count

        scope_identifier: None | str | Unset
        if isinstance(self.scope_identifier, Unset):
            scope_identifier = UNSET
        else:
            scope_identifier = self.scope_identifier

        agent_type: None | str | Unset
        if isinstance(self.agent_type, Unset):
            agent_type = UNSET
        else:
            agent_type = self.agent_type

        cooldown_seconds: int | None | Unset
        if isinstance(self.cooldown_seconds, Unset):
            cooldown_seconds = UNSET
        else:
            cooldown_seconds = self.cooldown_seconds

        cooldown_remaining: float | None | Unset
        if isinstance(self.cooldown_remaining, Unset):
            cooldown_remaining = UNSET
        else:
            cooldown_remaining = self.cooldown_remaining

        max_tokens: int | None | Unset
        if isinstance(self.max_tokens, Unset):
            max_tokens = UNSET
        else:
            max_tokens = self.max_tokens

        enabled = self.enabled

        last_run: dict[str, Any] | None | Unset
        if isinstance(self.last_run, Unset):
            last_run = UNSET
        elif isinstance(self.last_run, PlaybookLastRun):
            last_run = self.last_run.to_dict()
        else:
            last_run = self.last_run

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "scope": scope,
            }
        )
        if triggers is not UNSET:
            field_dict["triggers"] = triggers
        if version is not UNSET:
            field_dict["version"] = version
        if compiled_at is not UNSET:
            field_dict["compiled_at"] = compiled_at
        if node_count is not UNSET:
            field_dict["node_count"] = node_count
        if status is not UNSET:
            field_dict["status"] = status
        if running_count is not UNSET:
            field_dict["running_count"] = running_count
        if scope_identifier is not UNSET:
            field_dict["scope_identifier"] = scope_identifier
        if agent_type is not UNSET:
            field_dict["agent_type"] = agent_type
        if cooldown_seconds is not UNSET:
            field_dict["cooldown_seconds"] = cooldown_seconds
        if cooldown_remaining is not UNSET:
            field_dict["cooldown_remaining"] = cooldown_remaining
        if max_tokens is not UNSET:
            field_dict["max_tokens"] = max_tokens
        if enabled is not UNSET:
            field_dict["enabled"] = enabled
        if last_run is not UNSET:
            field_dict["last_run"] = last_run

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_last_run import PlaybookLastRun  # noqa: PLC0415

        d = dict(src_dict)
        id = d.pop("id")

        scope = d.pop("scope")

        triggers = cast(list[str], d.pop("triggers", UNSET))

        version = d.pop("version", UNSET)

        def _parse_compiled_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        compiled_at = _parse_compiled_at(d.pop("compiled_at", UNSET))

        node_count = d.pop("node_count", UNSET)

        status = d.pop("status", UNSET)

        running_count = d.pop("running_count", UNSET)

        def _parse_scope_identifier(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        scope_identifier = _parse_scope_identifier(d.pop("scope_identifier", UNSET))

        def _parse_agent_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        agent_type = _parse_agent_type(d.pop("agent_type", UNSET))

        def _parse_cooldown_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        cooldown_seconds = _parse_cooldown_seconds(d.pop("cooldown_seconds", UNSET))

        def _parse_cooldown_remaining(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        cooldown_remaining = _parse_cooldown_remaining(d.pop("cooldown_remaining", UNSET))

        def _parse_max_tokens(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_tokens = _parse_max_tokens(d.pop("max_tokens", UNSET))

        enabled = d.pop("enabled", UNSET)

        def _parse_last_run(data: object) -> None | PlaybookLastRun | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                last_run_type_0 = PlaybookLastRun.from_dict(data)

                return last_run_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlaybookLastRun | Unset, data)

        last_run = _parse_last_run(d.pop("last_run", UNSET))

        playbook_summary = cls(
            id=id,
            scope=scope,
            triggers=triggers,
            version=version,
            compiled_at=compiled_at,
            node_count=node_count,
            status=status,
            running_count=running_count,
            scope_identifier=scope_identifier,
            agent_type=agent_type,
            cooldown_seconds=cooldown_seconds,
            cooldown_remaining=cooldown_remaining,
            max_tokens=max_tokens,
            enabled=enabled,
            last_run=last_run,
        )

        playbook_summary.additional_properties = d
        return playbook_summary

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
