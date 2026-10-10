from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.agent_config_codex_service_tier_type_0 import AgentConfigCodexServiceTierType0
from ..models.agent_config_lifecycle_type_0 import AgentConfigLifecycleType0
from ..types import UNSET, Unset

T = TypeVar("T", bound="AgentConfig")


@_attrs_define
class AgentConfig:
    """
    Attributes:
        harness (None | str | Unset):
        default_class (None | str | Unset):
        needs_workspace (bool | None | Unset):
        lifecycle (AgentConfigLifecycleType0 | None | Unset):
        workspaces (list[str] | None | Unset):
        permission_mode (None | str | Unset):
        codex_full_auto (bool | None | Unset):
        codex_service_tier (AgentConfigCodexServiceTierType0 | None | Unset):
        claude_dangerously_skip_permissions (bool | None | Unset):
        read_only (bool | None | Unset):
    """

    harness: None | str | Unset = UNSET
    default_class: None | str | Unset = UNSET
    needs_workspace: bool | None | Unset = UNSET
    lifecycle: AgentConfigLifecycleType0 | None | Unset = UNSET
    workspaces: list[str] | None | Unset = UNSET
    permission_mode: None | str | Unset = UNSET
    codex_full_auto: bool | None | Unset = UNSET
    codex_service_tier: AgentConfigCodexServiceTierType0 | None | Unset = UNSET
    claude_dangerously_skip_permissions: bool | None | Unset = UNSET
    read_only: bool | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        harness: None | str | Unset
        if isinstance(self.harness, Unset):
            harness = UNSET
        else:
            harness = self.harness

        default_class: None | str | Unset
        if isinstance(self.default_class, Unset):
            default_class = UNSET
        else:
            default_class = self.default_class

        needs_workspace: bool | None | Unset
        if isinstance(self.needs_workspace, Unset):
            needs_workspace = UNSET
        else:
            needs_workspace = self.needs_workspace

        lifecycle: None | str | Unset
        if isinstance(self.lifecycle, Unset):
            lifecycle = UNSET
        elif isinstance(self.lifecycle, AgentConfigLifecycleType0):
            lifecycle = self.lifecycle.value
        else:
            lifecycle = self.lifecycle

        workspaces: list[str] | None | Unset
        if isinstance(self.workspaces, Unset):
            workspaces = UNSET
        elif isinstance(self.workspaces, list):
            workspaces = self.workspaces

        else:
            workspaces = self.workspaces

        permission_mode: None | str | Unset
        if isinstance(self.permission_mode, Unset):
            permission_mode = UNSET
        else:
            permission_mode = self.permission_mode

        codex_full_auto: bool | None | Unset
        if isinstance(self.codex_full_auto, Unset):
            codex_full_auto = UNSET
        else:
            codex_full_auto = self.codex_full_auto

        codex_service_tier: None | str | Unset
        if isinstance(self.codex_service_tier, Unset):
            codex_service_tier = UNSET
        elif isinstance(self.codex_service_tier, AgentConfigCodexServiceTierType0):
            codex_service_tier = self.codex_service_tier.value
        else:
            codex_service_tier = self.codex_service_tier

        claude_dangerously_skip_permissions: bool | None | Unset
        if isinstance(self.claude_dangerously_skip_permissions, Unset):
            claude_dangerously_skip_permissions = UNSET
        else:
            claude_dangerously_skip_permissions = self.claude_dangerously_skip_permissions

        read_only: bool | None | Unset
        if isinstance(self.read_only, Unset):
            read_only = UNSET
        else:
            read_only = self.read_only

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if harness is not UNSET:
            field_dict["harness"] = harness
        if default_class is not UNSET:
            field_dict["default_class"] = default_class
        if needs_workspace is not UNSET:
            field_dict["needs_workspace"] = needs_workspace
        if lifecycle is not UNSET:
            field_dict["lifecycle"] = lifecycle
        if workspaces is not UNSET:
            field_dict["workspaces"] = workspaces
        if permission_mode is not UNSET:
            field_dict["permission_mode"] = permission_mode
        if codex_full_auto is not UNSET:
            field_dict["codex_full_auto"] = codex_full_auto
        if codex_service_tier is not UNSET:
            field_dict["codex_service_tier"] = codex_service_tier
        if claude_dangerously_skip_permissions is not UNSET:
            field_dict["claude_dangerously_skip_permissions"] = claude_dangerously_skip_permissions
        if read_only is not UNSET:
            field_dict["read_only"] = read_only

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_harness(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        harness = _parse_harness(d.pop("harness", UNSET))

        def _parse_default_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        default_class = _parse_default_class(d.pop("default_class", UNSET))

        def _parse_needs_workspace(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        needs_workspace = _parse_needs_workspace(d.pop("needs_workspace", UNSET))

        def _parse_lifecycle(data: object) -> AgentConfigLifecycleType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                lifecycle_type_0 = AgentConfigLifecycleType0(data)

                return lifecycle_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AgentConfigLifecycleType0 | None | Unset, data)

        lifecycle = _parse_lifecycle(d.pop("lifecycle", UNSET))

        def _parse_workspaces(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                workspaces_type_0 = cast(list[str], data)

                return workspaces_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        workspaces = _parse_workspaces(d.pop("workspaces", UNSET))

        def _parse_permission_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        permission_mode = _parse_permission_mode(d.pop("permission_mode", UNSET))

        def _parse_codex_full_auto(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        codex_full_auto = _parse_codex_full_auto(d.pop("codex_full_auto", UNSET))

        def _parse_codex_service_tier(data: object) -> AgentConfigCodexServiceTierType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                codex_service_tier_type_0 = AgentConfigCodexServiceTierType0(data)

                return codex_service_tier_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AgentConfigCodexServiceTierType0 | None | Unset, data)

        codex_service_tier = _parse_codex_service_tier(d.pop("codex_service_tier", UNSET))

        def _parse_claude_dangerously_skip_permissions(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        claude_dangerously_skip_permissions = _parse_claude_dangerously_skip_permissions(
            d.pop("claude_dangerously_skip_permissions", UNSET)
        )

        def _parse_read_only(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        read_only = _parse_read_only(d.pop("read_only", UNSET))

        agent_config = cls(
            harness=harness,
            default_class=default_class,
            needs_workspace=needs_workspace,
            lifecycle=lifecycle,
            workspaces=workspaces,
            permission_mode=permission_mode,
            codex_full_auto=codex_full_auto,
            codex_service_tier=codex_service_tier,
            claude_dangerously_skip_permissions=claude_dangerously_skip_permissions,
            read_only=read_only,
        )

        return agent_config
