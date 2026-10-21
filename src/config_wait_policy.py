"""Dormant verified-wait policy; resolved afresh for every service decision."""

import copy
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace


@dataclass
class WaitKindPolicy:
    """Per-kind deadlines and duration estimates; None inherits in an override."""

    escalate_after_seconds: int | None = None
    expected_duration_seconds: int | None = None


def default_kinds():
    return {
        kind: WaitKindPolicy(escalate, expected)
        for kind, escalate, expected in (
            ("task", 14400, 3600), ("dependency", 14400, 3600),
            ("pull_request", 14400, 3600), ("batch", 14400, 2700),
            ("job", 7200, 900), ("message", 7200, 1800),
            ("playbook_condition", 7200, 1800), ("ci_check", 5400, 1200),
            ("ci_run", 5400, 1500), ("resource", 1800, None),
            # Timer escalation starts at due_at; its estimate is computed at decision time.
            ("timer", 300, None), ("human", None, None),
        )
    }


@dataclass
class WaitProjectOverrides:
    """Reviewed project policy; absent values inherit the global setting.

    Flags are nullable so a canary can enable v2 without enabling other projects.
    """

    in_place_ceiling_seconds: int | None = None
    resume_cost_minutes: float | None = None
    hibernate_margin: float | None = None
    v2_enabled: bool | None = None
    hibernation_enabled: bool | None = None
    scheduler_waits_enabled: bool | None = None
    kinds: dict[str, WaitKindPolicy] = field(default_factory=dict)


@dataclass
class WaitMetricsConfig:
    """An operator supplied historical cohort; rates are turns per minute."""

    pre_rollout_turn_rate: float | None = None
    pre_rollout_tokens_per_turn: float | None = None
    cohort: str | None = None
    window: str | None = None


@dataclass
class WaitPolicyConfig:
    """Wait defaults and project overlays, read at decision time.

    Rollout flags remain off until cutover. Registry consumers snapshot flags
    once per cycle, so hot reload cannot change an in-flight transaction.
    """

    v2_enabled: bool = False
    hibernation_enabled: bool = False
    scheduler_waits_enabled: bool = False
    v1_bodies: str = "translate"
    kinds: dict[str, WaitKindPolicy] = field(default_factory=default_kinds)
    in_place_ceiling_seconds: int = 1800
    hibernate_margin: float = 2.0
    resume_cost_minutes: float = 10
    min_samples: int = 5
    loop_window_registrations: int = 3
    loop_window_seconds: int = 3600
    branch_admission_deadline_seconds: int = 1800
    transition_budget_seconds: int = 120
    resume_launch_attempts: int = 2
    projects: dict[str, WaitProjectOverrides] = field(default_factory=dict)
    metrics: WaitMetricsConfig = field(default_factory=WaitMetricsConfig)

    def escalation_seconds(self, kind):
        return self.kinds[kind].escalate_after_seconds

    def resolve_project(self, project_id):
        """Return an independent effective policy: project > global > defaults."""
        resolved = copy.deepcopy(self)
        resolved.projects = {}
        # A directly constructed config can supply only a subset of kinds too.
        resolved.kinds = {**default_kinds(), **resolved.kinds}
        override = self.projects.get(project_id)
        if override is None:
            return resolved
        for item in fields(override):
            value = getattr(override, item.name)
            if item.name == "kinds":
                for kind, policy in value.items():
                    resolved.kinds[kind] = replace(resolved.kinds[kind], **{
                        f.name: getattr(policy, f.name) for f in fields(policy)
                        if getattr(policy, f.name) is not None
                    })
            elif value is not None:
                setattr(resolved, item.name, value)
        return resolved

    def validate(self):
        from src.config import ConfigError

        errors = _validate_settings(self, "", optional=False)

        def error(key, message):
            errors.append(ConfigError("wait_policy", key, message))

        if self.v1_bodies not in {"translate", "refuse"}:
            error("v1_bodies", "must be translate or refuse")
        if not isinstance(self.projects, dict):
            error("projects", "must be a mapping")
        else:
            for pid, override in self.projects.items():
                path = f"projects.{pid}"
                if not isinstance(pid, str) or not pid.strip():
                    error(path, "must be a project id")
                if not isinstance(override, WaitProjectOverrides):
                    error(path, "must be project overrides")
                else:
                    errors.extend(_validate_settings(override, path, optional=True))
        for name in ("pre_rollout_turn_rate", "pre_rollout_tokens_per_turn"):
            value = getattr(self.metrics, name)
            if value is not None and (
                type(value) not in (int, float) or not math.isfinite(value) or value < 0
            ):
                error(f"metrics.{name}", "must be finite and >= 0")
        for name in ("cohort", "window"):
            value = getattr(self.metrics, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                error(f"metrics.{name}", "must be a non-empty string")
        return errors


# Bounds for the integer settings: (minimum, maximum or None).
_INTEGER_BOUNDS = {
    "in_place_ceiling_seconds": (60, 86400),
    "loop_window_seconds": (60, 86400),
    "branch_admission_deadline_seconds": (60, 86400),
    "transition_budget_seconds": (1, 86400),
    "min_samples": (1, None),
    "loop_window_registrations": (1, None),
    "resume_launch_attempts": (1, None),
}


def _bad_integer(value, minimum, maximum):
    return type(value) is not int or value < minimum or (maximum is not None and value > maximum)


def _validate_settings(settings, path, *, optional):
    from src.config import ConfigError

    errors = []

    def error(key, message):
        errors.append(ConfigError("wait_policy", key, message))

    for item in fields(settings):
        name = item.name
        value = getattr(settings, name)
        key = f"{path}.{name}" if path else name
        if name in ("kinds", "projects", "metrics", "v1_bodies") or (optional and value is None):
            continue
        if name.endswith("_enabled"):
            if type(value) is not bool:
                error(key, "must be a boolean")
        elif name in ("hibernate_margin", "resume_cost_minutes"):
            low = 1.0 if name == "hibernate_margin" else 0.0
            if (
                type(value) not in (int, float) or not math.isfinite(value)
                or value < low or (name == "resume_cost_minutes" and value == 0)
            ):
                error(key, "must be finite and >= 1" if low else "must be finite and > 0")
        elif name in _INTEGER_BOUNDS:
            low, high = _INTEGER_BOUNDS[name]
            if _bad_integer(value, low, high):
                error(key, f"must be {low}–{high}" if high else "must be > 0")

    kind_path = f"{path}.kinds" if path else "kinds"
    if not isinstance(settings.kinds, dict):
        error(kind_path, "must be a mapping")
        return errors
    defaults = default_kinds()
    for kind, policy in settings.kinds.items():
        key = f"{kind_path}.{kind}"
        if kind not in defaults:
            error(key, "unknown wait kind")
        elif not isinstance(policy, WaitKindPolicy):
            error(key, "must be a kind policy")
        elif kind == "human" and (optional or policy != defaults["human"]):
            error(key, "human waits have no escalation policy")
        else:
            for name, low, high in (
                ("escalate_after_seconds", 60, 86400), ("expected_duration_seconds", 0, None),
            ):
                value = getattr(policy, name)
                if value is not None and _bad_integer(value, low, high):
                    error(f"{key}.{name}", f"must be {low}–{high}" if high else f"must be >= {low}")
            if not optional and kind != "human" and policy.escalate_after_seconds is None:
                error(key, "requires an escalation deadline")
    return errors


def parse_wait_policy(raw):
    """Parse the section strictly: unknown keys and non-mapping shapes are errors.

    Values are kept as written so ``validate`` reports them; a string 'false'
    must never activate the services.
    """
    from src.config import ConfigError, ConfigValidationError

    errors = []

    def parse(cls, values, path, base=None):
        instance = base if base is not None else cls()
        if not isinstance(values, Mapping):
            errors.append(ConfigError("wait_policy", path or "wait_policy", "must be a mapping"))
            return instance
        known = {f.name for f in fields(cls)}
        for name, value in values.items():
            key = f"{path}.{name}" if path else str(name)
            if name not in known:
                errors.append(ConfigError("wait_policy", key, "unknown key"))
            elif name == "metrics":
                instance.metrics = parse(WaitMetricsConfig, value, key)
            elif name in ("kinds", "projects"):
                if not isinstance(value, Mapping):
                    errors.append(ConfigError("wait_policy", key, "must be a mapping"))
                    continue
                rows = getattr(instance, name)
                row_cls = WaitKindPolicy if name == "kinds" else WaitProjectOverrides
                for row_id, row in value.items():
                    row_path = f"{key}.{row_id}"
                    if name == "kinds" and row_id == "human" and (
                        cls is WaitProjectOverrides or row not in (
                            {}, {"escalate_after_seconds": None, "expected_duration_seconds": None},
                        )
                    ):
                        errors.append(ConfigError(
                            "wait_policy", row_path, "human waits have no escalation policy",
                        ))
                        continue
                    # Partial global kind entries keep the omitted defaults.
                    rows[row_id] = parse(row_cls, row, row_path, rows.get(row_id))
            else:
                setattr(instance, name, value)
        return instance

    config = parse(WaitPolicyConfig, raw, "")
    if errors:
        raise ConfigValidationError([str(error) for error in errors])
    return config
