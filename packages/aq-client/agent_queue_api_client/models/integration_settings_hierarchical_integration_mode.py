from enum import Enum


class IntegrationSettingsHierarchicalIntegrationMode(str, Enum):
    DISABLED = "disabled"
    HIERARCHY = "hierarchy"
    OBSERVE = "observe"
    TRAIN = "train"

    def __str__(self) -> str:
        return str(self.value)
