from enum import Enum


class ShellPreferencesTheme(str, Enum):
    DARK = "dark"
    LIGHT = "light"
    SYSTEM = "system"

    def __str__(self) -> str:
        return str(self.value)
