from enum import Enum


class ItemType(str, Enum):
    AGENT_SETTINGS = "agent_settings"
    CI_TEST = "ci_test"
    PLAYBOOK = "playbook"
    PROMOTION_FLOW = "promotion_flow"
    ROUTING = "routing"
    TEMPLATE = "template"

    def __str__(self) -> str:
        return str(self.value)
