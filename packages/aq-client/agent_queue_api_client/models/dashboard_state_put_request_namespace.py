from enum import Enum


class DashboardStatePutRequestNamespace(str, Enum):
    COMMAND_CENTER_PREFERENCES = "command_center_preferences"
    COMMAND_CENTER_PROJECT_VIEW = "command_center_project_view"
    NAV_ORGANIZATION = "nav_organization"
    PLAYBOOK_GRAPH_VIEW = "playbook_graph_view"
    SHELL_PREFERENCES = "shell_preferences"

    def __str__(self) -> str:
        return str(self.value)
