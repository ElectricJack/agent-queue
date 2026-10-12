from enum import Enum


class DiffItemType(str, Enum):
    AGENT_SETTINGS = "agent_settings"
    CI_TEST = "ci_test"
    INTELLIGENCE_CLASS = "intelligence_class"
    PLAYBOOK = "playbook"
    POOL = "pool"
    PROJECT_SETTINGS = "project_settings"
    PROMOTION_FLOW = "promotion_flow"
    ROUTING = "routing"
    TEMPLATE = "template"

    def __str__(self) -> str:
        return str(self.value)
