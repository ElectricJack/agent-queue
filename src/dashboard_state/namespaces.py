"""Registry for every server-backed dashboard state namespace."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

from pydantic import BaseModel

from src.api.models.dashboard import (
    CommandCenterPreferences,
    CommandCenterProjectView,
    NavOrganization,
    PlaybookGraphView,
    ShellPreferences,
)


@dataclass(frozen=True, slots=True)
class NamespaceSpec:
    name: str
    scope: Literal["workspace", "user"]
    subject: Literal["none", "project"]
    write_mode: Literal["cas", "lww"]
    model: type[BaseModel]
    inventory_name: str

    def default_value(self) -> dict:
        return self.model().model_dump(mode="json")


NAMESPACES: Mapping[str, NamespaceSpec] = MappingProxyType(
    {
        "nav_organization": NamespaceSpec(
            name="nav_organization",
            scope="workspace",
            subject="none",
            write_mode="cas",
            model=NavOrganization,
            inventory_name="workspace.nav_organization",
        ),
        "shell_preferences": NamespaceSpec(
            name="shell_preferences",
            scope="user",
            subject="none",
            write_mode="lww",
            model=ShellPreferences,
            inventory_name="user.shell_preferences",
        ),
        "command_center_preferences": NamespaceSpec(
            name="command_center_preferences",
            scope="user",
            subject="none",
            write_mode="lww",
            model=CommandCenterPreferences,
            inventory_name="user.command_center_preferences",
        ),
        "command_center_project_view": NamespaceSpec(
            name="command_center_project_view",
            scope="user",
            subject="project",
            write_mode="cas",
            model=CommandCenterProjectView,
            inventory_name="user.command_center_preferences.projects[project_id]",
        ),
        "playbook_graph_view": NamespaceSpec(
            name="playbook_graph_view",
            scope="user",
            subject="none",
            write_mode="cas",
            model=PlaybookGraphView,
            inventory_name="user.command_center_preferences.views.playbooks",
        ),
    }
)
