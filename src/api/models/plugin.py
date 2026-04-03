"""Response models for plugin commands."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class PluginSummary(BaseModel):
    name: str
    version: str = ""
    status: str = ""
    source_url: str = ""
    description: str | None = None
    commands: list[Any] | None = None
    tools: list[Any] | None = None


class PluginListResponse(BaseModel):
    plugins: list[PluginSummary] = []
    count: int = 0


class PluginInfoResponse(BaseModel):
    plugin: dict[str, Any] = {}


class PluginInstallResponse(BaseModel):
    installed: str
    message: str = ""


class PluginRemoveResponse(BaseModel):
    removed: str
    message: str = ""


class PluginUpdateResponse(BaseModel):
    updated: str
    rev: str = ""
    message: str = ""


class PluginEnableResponse(BaseModel):
    enabled: str
    message: str = ""


class PluginDisableResponse(BaseModel):
    disabled: str
    message: str = ""


class PluginReloadResponse(BaseModel):
    reloaded: str
    message: str = ""


class PluginConfigResponse(BaseModel):
    name: str
    config: dict[str, Any] = {}
    message: str | None = None


class PluginPromptsResponse(BaseModel):
    name: str
    prompts: list[Any] = []


class PluginResetPromptsResponse(BaseModel):
    name: str
    reset_count: int = 0
    message: str = ""


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "plugin_list": PluginListResponse,
    "plugin_info": PluginInfoResponse,
    "plugin_install": PluginInstallResponse,
    "plugin_remove": PluginRemoveResponse,
    "plugin_update": PluginUpdateResponse,
    "plugin_enable": PluginEnableResponse,
    "plugin_disable": PluginDisableResponse,
    "plugin_reload": PluginReloadResponse,
    "plugin_config": PluginConfigResponse,
    "plugin_prompts": PluginPromptsResponse,
    "plugin_reset_prompts": PluginResetPromptsResponse,
}
