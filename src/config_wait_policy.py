"""Dormant verified-wait policy; resolved afresh for every service decision."""

from dataclasses import dataclass, field, fields
import math


@dataclass
class WaitKindPolicy:
    escalate_after_seconds: float | None = None
    expected_duration_seconds: float | None = None


def default_kinds():
    return {
        kind: WaitKindPolicy(escalate, expected)
        for kind, escalate, expected in (
            ("task", 14400, 3600), ("dependency", 14400, 3600),
            ("pull_request", 14400, 3600), ("batch", 14400, 2700),
            ("job", 7200, 900), ("message", 7200, 1800),
            ("playbook_condition", 7200, 1800), ("ci_check", 5400, 1200),
            ("ci_run", 5400, 1500), ("resource", 1800, None),
            ("timer", 300, None), ("human", None, None),
        )
    }


@dataclass
class WaitProjectOverrides:
    in_place_ceiling_seconds: float | None = None
    resume_cost_minutes: float | None = None
    hibernate_margin: float | None = None
    kinds: dict[str, WaitKindPolicy] = field(default_factory=dict)


@dataclass
class WaitPolicyConfig:
    v2_enabled: bool = False
    hibernation_enabled: bool = False
    scheduler_waits_enabled: bool = False
    v1_bodies: str = "translate"
    kinds: dict[str, WaitKindPolicy] = field(default_factory=default_kinds)
    in_place_ceiling_seconds: float = 1800
    hibernate_margin: float = 2.0
    resume_cost_minutes: float = 10
    min_samples: int = 5
    loop_window_registrations: int = 3
    loop_window_seconds: float = 3600
    branch_admission_deadline_seconds: float = 1800
    transition_budget_seconds: float = 120
    resume_launch_attempts: int = 2
    projects: dict[str, WaitProjectOverrides] = field(default_factory=dict)

    def escalation_seconds(self, kind):
        return self.kinds[kind].escalate_after_seconds

    def validate(self):
        from src.config import ConfigError

        errors = []

        def error(key, message):
            errors.append(ConfigError("wait_policy", key, message))

        def number(key, value, low, high=86400):
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                error(key, f"must be between {low} and {high}")

        for name in ("v2_enabled", "hibernation_enabled", "scheduler_waits_enabled"):
            if type(getattr(self, name)) is not bool:
                error(name, "must be a boolean")
        if self.v1_bodies not in {"translate", "refuse"}:
            error("v1_bodies", "must be translate or refuse")
        for name in ("in_place_ceiling_seconds", "branch_admission_deadline_seconds"):
            number(name, getattr(self, name), 60)
        for name in ("hibernate_margin", "resume_cost_minutes", "min_samples",
                     "loop_window_registrations", "loop_window_seconds",
                     "transition_budget_seconds", "resume_launch_attempts"):
            number(name, getattr(self, name), 1)
        for prefix, kinds in [("kinds", self.kinds), *(
            (f"projects.{pid}.kinds", override.kinds) for pid, override in self.projects.items()
        )]:
            for kind, policy in kinds.items():
                if kind not in default_kinds() or kind == "human" and policy != WaitKindPolicy():
                    error(f"{prefix}.{kind}", "unknown kind or human escalation override")
                    continue
                for name in ("escalate_after_seconds", "expected_duration_seconds"):
                    value = getattr(policy, name)
                    if value is not None:
                        number(f"{prefix}.{kind}.{name}", value, 1)
        for pid, override in self.projects.items():
            for name, low in (("in_place_ceiling_seconds", 60), ("resume_cost_minutes", 1),
                              ("hibernate_margin", 1)):
                value = getattr(override, name)
                if value is not None:
                    number(f"projects.{pid}.{name}", value, low)
        return errors


def parse_wait_policy(raw):
    """Reject unknown keys; string 'false' must never activate the services."""
    known = {f.name for f in fields(WaitPolicyConfig)}
    if not isinstance(raw, dict) or set(raw) - known:
        raise ValueError("invalid wait_policy section")
    values = {k: v for k, v in raw.items() if k not in {"kinds", "projects"}}
    kinds = default_kinds()
    for name, policy in raw.get("kinds", {}).items():
        base = kinds.get(name, WaitKindPolicy())
        kinds[name] = WaitKindPolicy(**{
            "escalate_after_seconds": base.escalate_after_seconds,
            "expected_duration_seconds": base.expected_duration_seconds, **policy,
        })
    overrides = {}
    for pid, override in raw.get("projects", {}).items():
        overrides[pid] = WaitProjectOverrides(**{
            **override, "kinds": {
                k: WaitKindPolicy(**v) for k, v in override.get("kinds", {}).items()
            },
        })
    return WaitPolicyConfig(**values, kinds=kinds, projects=overrides)
