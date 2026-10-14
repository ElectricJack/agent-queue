from enum import Enum


class ReceiptDTOStepKind(str, Enum):
    AGENT_TASK = "agent_task"
    COMMAND = "command"
    DECISION = "decision"
    FOREACH = "foreach"
    LLM = "llm"
    SCRIPT = "script"
    TERMINAL = "terminal"
    WAIT = "wait"

    def __str__(self) -> str:
        return str(self.value)
