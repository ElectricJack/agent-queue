from enum import Enum


class EffectiveGitIdentitySource(str, Enum):
    FALLBACK = "fallback"
    INSTALLATION = "installation"
    PROJECT = "project"

    def __str__(self) -> str:
        return str(self.value)
