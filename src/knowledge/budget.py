"""One hard gate for the assembled AQ prompt, independent of provider plugins.

UTF-8 bytes are a conservative token upper bound. This intentionally does not
reuse the historical characters/4 display estimate. Required instructions are
never truncated: callers must refuse delivery when they do not fit.
"""

from dataclasses import asdict, dataclass
import json

from src.records.models import RecordError


@dataclass(frozen=True)
class ContextBudget:
    input_tokens: int = 32768
    output_tokens: int = 4096
    wrapper_tokens: int = 256
    knowledge_tokens: int = 4096
    knowledge_bytes: int = 16384
    discovery_tokens: int = 1000
    discovery_items: int = 8
    method: str = "upper_bound_bytes"

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key != "method" and (type(value) is not int or value < 0):
                raise ValueError(f"Invalid context budget: {key}")
        if self.method != "upper_bound_bytes":
            raise ValueError("Unsupported tokenizer")

    @classmethod
    def from_config(cls, config, *, remaining_tokens=None):
        settings = config.knowledge.context
        limit = getattr(settings, "input_max_tokens", 32768)
        if remaining_tokens is not None:
            if type(remaining_tokens) is not int or remaining_tokens < 0:
                raise ValueError("remaining_tokens must be a nonnegative integer")
            limit = min(limit, remaining_tokens)
        return cls(
            input_tokens=limit,
            output_tokens=getattr(settings, "output_reserve_tokens", 4096),
            wrapper_tokens=getattr(settings, "wrapper_reserve_tokens", 256),
            knowledge_tokens=min(
                config.memory.context_max_tokens, getattr(settings, "max_tokens", 4096)
            ),
            knowledge_bytes=getattr(settings, "max_bytes", 16384),
            discovery_tokens=getattr(settings, "discovery_max_tokens", 1000),
            discovery_items=getattr(settings, "discovery_max_items", 8),
        )

    def account(self, required: str, knowledge: str = "", *, tools=()) -> dict:
        tool_text = json.dumps(tools, ensure_ascii=False, sort_keys=True) if tools else ""
        required_tokens = len(required.encode("utf-8")) + len(tool_text.encode("utf-8"))
        knowledge_bytes = len(knowledge.encode("utf-8"))
        reserved = self.output_tokens + self.wrapper_tokens
        effective = max(0, min(self.knowledge_tokens, self.input_tokens - required_tokens - reserved))
        required_fits = required_tokens + reserved <= self.input_tokens
        return {
            "requested": asdict(self),
            "effective_knowledge_tokens": effective,
            "required_tokens": required_tokens,
            "knowledge_tokens": knowledge_bytes,
            "knowledge_bytes": knowledge_bytes,
            "total_tokens": required_tokens + knowledge_bytes + reserved,
            "method": self.method,
            "fits": required_fits and knowledge_bytes <= min(effective, self.knowledge_bytes),
            "diagnostic": None if required_fits else "context.required_over_budget",
        }

    def enforce(self, required: str, knowledge: str = "", *, tools=()) -> dict:
        result = self.account(required, knowledge, tools=tools)
        if not result["fits"]:
            raise RecordError(result["diagnostic"] or "context.over_budget")
        return result
