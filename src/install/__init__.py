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

from .command import CommandOutput, CommandRunner, run_command
from .engine import (
    ConsentCallback,
    InstallEngine,
    InstallOptions,
    ProgressCallback,
    ProgressEvent,
    run_install,
)
from .logins import (
    AuthProbe,
    CredentialStore,
    EnvironmentCredential,
    ProviderLogin,
    login_step,
    login_steps,
    probe_all,
    probe_login,
    provider_logins,
)
from .macos import macos_steps
from .onboarding import (
    CAPABILITY_DAEMON,
    CAPABILITY_DISCORD,
    DashboardInfo,
    HttpProbe,
    Location,
    api_base_url,
    data_locations,
    http_status,
    inspect_dashboard,
    onboarding_steps,
)
from .platform import (
    PlatformFacts,
    SupportVerdict,
    describe_host,
    detect_platform,
    evaluate_support,
)
from .postgres import (
    MINIMUM_SERVER_VERSION,
    ConnectionCheck,
    PostgresSettings,
    PostgresSettingsError,
)
from .postgres_steps import (
    CAPABILITY_MANAGED,
    CAPABILITY_ROTATE,
    PostgresAdapter,
    postgres_steps,
)
from .prerequisites import default_registry, prerequisite_steps
from .providers import (
    ExecutableProbe,
    ProviderInstaller,
    probe_executable,
    provider_installers,
    provider_step,
    provider_steps,
)
from .redaction import SecretLeakError, assert_secret_free, redact
from .registry import build_registry, platform_steps
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
from .wizard import (
    OnboardingSummary,
    Question,
    WizardChoices,
    capabilities_for,
    question_plan,
    summarize,
)

__all__ = [
    "CAPABILITY_DAEMON",
    "CAPABILITY_DISCORD",
    "CAPABILITY_MANAGED",
    "CAPABILITY_ROTATE",
    "EXIT_CODES",
    "MINIMUM_SERVER_VERSION",
    "RESULT_SCHEMA_VERSION",
    "STATE_SCHEMA_VERSION",
    "AuthProbe",
    "CommandOutput",
    "CommandRunner",
    "ConnectionCheck",
    "ConsentCallback",
    "CredentialStore",
    "DashboardInfo",
    "EnvironmentCredential",
    "ExecutableProbe",
    "HttpProbe",
    "IncompatibleStateError",
    "InstallEngine",
    "InstallOptions",
    "InstallOutcome",
    "InstallPlanError",
    "InstallResult",
    "InstallState",
    "Location",
    "OnboardingSummary",
    "PlanAction",
    "PlannedStep",
    "PlatformFacts",
    "PostgresAdapter",
    "PostgresSettings",
    "PostgresSettingsError",
    "ProgressCallback",
    "ProgressEvent",
    "ProviderInstaller",
    "ProviderLogin",
    "Question",
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
    "WizardChoices",
    "api_base_url",
    "assert_secret_free",
    "build_registry",
    "capabilities_for",
    "data_locations",
    "default_registry",
    "default_state_path",
    "describe_host",
    "detect_platform",
    "evaluate_support",
    "exit_code",
    "http_status",
    "inspect_dashboard",
    "load_state",
    "login_step",
    "login_steps",
    "macos_steps",
    "onboarding_steps",
    "platform_steps",
    "postgres_steps",
    "prerequisite_steps",
    "probe_all",
    "probe_executable",
    "probe_login",
    "provider_installers",
    "provider_logins",
    "provider_step",
    "provider_steps",
    "question_plan",
    "redact",
    "run_command",
    "run_install",
    "save_state",
    "summarize",
]
