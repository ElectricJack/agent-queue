"""Shared installer orchestration for ``aq install``.

The engine here is the common interface named in
``docs/plans/install-onboarding/contract.md``: one step protocol, one
platform matrix, one secret-free resume record, one machine-readable result
and one exit-code table.  Platform, packaging, database and provider adapters
register :class:`~src.install.steps.StepSpec` values against the registry
rather than shipping installers of their own.

The published surface of the CLI it drives — flags, JSON payload and exit
codes — is ``docs/reference/cli/install.md``.
"""

from __future__ import annotations

from .engine import (
    ConsentCallback,
    InstallEngine,
    InstallOptions,
    ProgressCallback,
    ProgressEvent,
    run_install,
)
from .platform import (
    PlatformFacts,
    SupportVerdict,
    describe_host,
    detect_platform,
    evaluate_support,
)
from .prerequisites import default_registry
from .redaction import SecretLeakError, assert_secret_free, redact
from .results import (
    EXIT_CODES,
    RESULT_SCHEMA_VERSION,
    InstallOutcome,
    InstallResult,
    PlanAction,
    PlannedStep,
    ResourceRecord,
    StepResult,
    StepState,
    exit_code,
)
from .state import (
    STATE_SCHEMA_VERSION,
    IncompatibleStateError,
    InstallState,
    StateError,
    StepRecord,
    default_state_path,
    load_state,
    save_state,
)
from .steps import InstallPlanError, StepContext, StepRegistry, StepSpec

__all__ = [
    "EXIT_CODES",
    "RESULT_SCHEMA_VERSION",
    "STATE_SCHEMA_VERSION",
    "ConsentCallback",
    "IncompatibleStateError",
    "InstallEngine",
    "InstallOptions",
    "InstallOutcome",
    "InstallPlanError",
    "InstallResult",
    "InstallState",
    "PlanAction",
    "PlannedStep",
    "PlatformFacts",
    "ProgressCallback",
    "ProgressEvent",
    "ResourceRecord",
    "SecretLeakError",
    "StateError",
    "StepContext",
    "StepRecord",
    "StepRegistry",
    "StepResult",
    "StepSpec",
    "StepState",
    "SupportVerdict",
    "assert_secret_free",
    "default_registry",
    "default_state_path",
    "describe_host",
    "detect_platform",
    "evaluate_support",
    "exit_code",
    "load_state",
    "redact",
    "run_install",
    "save_state",
]
