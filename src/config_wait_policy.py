"""Verified-wait policy shared by configuration and runtime consumers."""

from __future__ import annotations

import copy
import dataclasses
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import ConfigError


@dataclass
class WaitKindPolicy:
    """Per-kind deadlines and duration estimates; None inherits in an override."""

    escalate_after_seconds: int | None = None
    expected_duration_seconds: int | None = None

    def validate(self, path: str = "kinds") -> list[ConfigError]:
        from src.config import ConfigError

        errors: list[ConfigError] = []
        for name, minimum, maximum in (
            ("escalate_after_seconds", 60, 86400),
            ("expected_duration_seconds", 0, None),
        ):
            value = getattr(self, name)
            if value is not None and (
                type(value) is not int or value < minimum
                or (maximum is not None and value > maximum)
            ):
                limit = f"{minimum}–{maximum}" if maximum is not None else f">= {minimum}"
                errors.append(ConfigError("wait_policy", f"{path}.{name}", f"must be {limit}"))
        return errors


def default_kinds() -> dict[str, WaitKindPolicy]:
    return {
        "task": WaitKindPolicy(14400, 3600),
        "dependency": WaitKindPolicy(14400, 3600),
        "pull_request": WaitKindPolicy(14400, 3600),
        "batch": WaitKindPolicy(14400, 2700),
        "job": WaitKindPolicy(7200, 900),
        "message": WaitKindPolicy(7200, 1800),
        "playbook_condition": WaitKindPolicy(7200, 1800),
        "ci_check": WaitKindPolicy(5400, 1200),
        "ci_run": WaitKindPolicy(5400, 1500),
        "resource": WaitKindPolicy(1800),
        # Timer escalation starts at due_at; its estimate is computed at decision time.
        "timer": WaitKindPolicy(300),
        "human": WaitKindPolicy(),
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

    def validate(self, path: str = "projects") -> list[ConfigError]:
        return _validate_wait_settings(self, path, optional=True)


@dataclass
class WaitMetricsConfig:
    """An operator supplied historical cohort; rates are turns per minute."""

    pre_rollout_turn_rate: float | None = None
    pre_rollout_tokens_per_turn: float | None = None
    cohort: str | None = None
    window: str | None = None

    def validate(self) -> list[ConfigError]:
        from src.config import ConfigError

        errors = []
        for name in ("pre_rollout_turn_rate", "pre_rollout_tokens_per_turn"):
            value = getattr(self, name)
            if value is not None and (
                type(value) not in (int, float) or not math.isfinite(value) or value < 0
            ):
                errors.append(ConfigError("wait_policy", f"metrics.{name}",
                                          "must be finite and nonnegative"))
        for name in ("cohort", "window"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                errors.append(ConfigError("wait_policy", f"metrics.{name}",
                                          "must be a non-empty string"))
        return errors


@dataclass
class WaitPolicyConfig:
    """Wait defaults and project overlays, read at decision time.

    Cutover defaults enable verified waits. Registry consumers snapshot flags
    once per cycle, so hot reload cannot change an in-flight transaction.
    """

    v2_enabled: bool = True
    hibernation_enabled: bool = True
    scheduler_waits_enabled: bool = True
    v1_bodies: str = "refuse"
    in_place_ceiling_seconds: int = 1800
    hibernate_margin: float = 2.0
    resume_cost_minutes: float = 10.0
    min_samples: int = 5
    loop_window_registrations: int = 3
    loop_window_seconds: int = 3600
    branch_admission_deadline_seconds: int = 1800
    transition_budget_seconds: int = 120
    resume_launch_attempts: int = 2
    kinds: dict[str, WaitKindPolicy] = field(default_factory=default_kinds)
    projects: dict[str, WaitProjectOverrides] = field(default_factory=dict)
    metrics: WaitMetricsConfig = field(default_factory=WaitMetricsConfig)

    def escalation_seconds(self, kind: str) -> int | None:
        return self.kinds[kind].escalate_after_seconds

    def validate(self) -> list[ConfigError]:
        from src.config import ConfigError

        errors = _validate_wait_settings(self, "", optional=False)
        if isinstance(self.metrics, WaitMetricsConfig):
            errors.extend(self.metrics.validate())
        else:
            errors.append(ConfigError("wait_policy", "metrics", "must be metrics settings"))
        if self.v1_bodies not in ("translate", "refuse"):
            errors.append(ConfigError("wait_policy", "v1_bodies", "must be translate or refuse"))
        if not isinstance(self.projects, dict):
            errors.append(ConfigError("wait_policy", "projects", "must be a mapping"))
        else:
            for project_id, override in self.projects.items():
                path = f"projects.{project_id}"
                if not isinstance(project_id, str) or not project_id.strip():
                    errors.append(ConfigError("wait_policy", path, "must be a project id"))
                if not isinstance(override, WaitProjectOverrides):
                    errors.append(ConfigError("wait_policy", path, "must be project overrides"))
                else:
                    errors.extend(override.validate(path))
        return errors

    def resolve_project(self, project_id: str) -> WaitPolicyConfig:
        """Return an independent effective policy: project > global > defaults."""
        resolved = copy.deepcopy(self)
        resolved.projects = {}
        # A directly constructed config can supply only a subset of kinds too.
        resolved.kinds = {**default_kinds(), **resolved.kinds}
        override = self.projects.get(project_id)
        if override is not None:
            for item in dataclasses.fields(override):
                value = getattr(override, item.name)
                if item.name == "kinds":
                    for kind, policy in value.items():
                        supplied = {
                            f.name: getattr(policy, f.name)
                            for f in dataclasses.fields(policy)
                            if getattr(policy, f.name) is not None
                        }
                        resolved.kinds[kind] = dataclasses.replace(resolved.kinds[kind], **supplied)
                elif value is not None:
                    setattr(resolved, item.name, value)
        return resolved


def _validate_wait_settings(
    settings: WaitPolicyConfig | WaitProjectOverrides, path: str, *, optional: bool,
) -> list[ConfigError]:
    from src.config import ConfigError

    errors: list[ConfigError] = []
    for item in dataclasses.fields(settings):
        name = item.name
        value = getattr(settings, name)
        key = f"{path}.{name}" if path else name
        if name in ("kinds", "projects", "v1_bodies", "metrics") or (optional and value is None):
            continue
        message = None
        if name.endswith("_enabled"):
            if type(value) is not bool:
                message = "must be a boolean"
        elif name in ("hibernate_margin", "resume_cost_minutes"):
            minimum = 1.0 if name == "hibernate_margin" else 0.0
            if (
                type(value) not in (int, float) or not math.isfinite(value)
                or value < minimum or (name == "resume_cost_minutes" and value == 0)
            ):
                message = "must be finite and >= 1" if minimum else "must be finite and > 0"
        else:
            minimum = 60 if name in (
                "in_place_ceiling_seconds", "loop_window_seconds",
                "branch_admission_deadline_seconds",
            ) else 1
            maximum = 86400 if name.endswith("_seconds") else None
            if (
                type(value) is not int or value < minimum
                or (maximum is not None and value > maximum)
            ):
                message = f"must be {minimum}–{maximum}" if maximum else "must be > 0"
        if message:
            errors.append(ConfigError("wait_policy", key, message))

    kind_path = f"{path}.kinds" if path else "kinds"
    if not isinstance(settings.kinds, dict):
        errors.append(ConfigError("wait_policy", kind_path, "must be a mapping"))
        return errors
    defaults = default_kinds()
    for kind, policy in settings.kinds.items():
        key = f"{kind_path}.{kind}"
        if kind not in defaults:
            errors.append(ConfigError("wait_policy", key, "unknown wait kind"))
        elif not isinstance(policy, WaitKindPolicy):
            errors.append(ConfigError("wait_policy", key, "must be a kind policy"))
        elif kind == "human" and (optional or policy != defaults["human"]):
            errors.append(ConfigError("wait_policy", key, "human waits have no escalation policy"))
        else:
            errors.extend(policy.validate(key))
            if not optional and kind != "human" and policy.escalate_after_seconds is None:
                errors.append(ConfigError("wait_policy", key, "requires an escalation deadline"))
    return errors


def parse_wait_policy(raw: object) -> WaitPolicyConfig:
    """Parse only the wait-policy tree strictly, keeping typed validation errors."""
    from src.config import ConfigError, ConfigValidationError

    errors: list[ConfigError] = []

    def parse(cls: type, values: object, path: str, base=None):
        instance = base if base is not None else cls()
        if not isinstance(values, Mapping):
            errors.append(ConfigError("wait_policy", path, "must be a mapping"))
            return instance
        fields = {f.name for f in dataclasses.fields(cls)}
        for name, value in values.items():
            key = f"{path}.{name}" if path else str(name)
            if name not in fields:
                errors.append(ConfigError("wait_policy", key, "unknown key"))
                continue
            if name == "metrics":
                instance.metrics = parse(WaitMetricsConfig, value, key)
            elif name in ("kinds", "projects"):
                if not isinstance(value, Mapping):
                    errors.append(ConfigError("wait_policy", key, "must be a mapping"))
                    continue
                rows = getattr(instance, name)
                row_cls = WaitKindPolicy if name == "kinds" else WaitProjectOverrides
                for row_id, row in value.items():
                    row_path = f"{key}.{row_id}"
                    if (
                        name == "kinds" and row_id == "human"
                        and (cls is WaitProjectOverrides or row not in (
                            {}, {"escalate_after_seconds": None, "expected_duration_seconds": None},
                        ))
                    ):
                        errors.append(ConfigError(
                            "wait_policy", row_path, "human waits have no escalation policy",
                        ))
                        continue
                    rows[row_id] = parse(row_cls, row, row_path, rows.get(row_id))
            else:
                setattr(instance, name, value)
        return instance

    config = parse(WaitPolicyConfig, raw, "")
    if errors:
        raise ConfigValidationError([str(error) for error in errors])
    return config
