from enum import Enum


class EffectClauseDTOKind(str, Enum):
    BINDS = "binds"
    BRANCHES = "branches"
    CREATES = "creates"
    DELEGATES = "delegates"
    DELETES = "deletes"
    INVOKES_AI = "invokes_ai"
    NOOP = "noop"
    READS = "reads"
    SCHEDULES = "schedules"
    SENDS = "sends"
    UPDATES = "updates"
    WAITS = "waits"

    def __str__(self) -> str:
        return str(self.value)
