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

from __future__ import annotations as annotations


__all__ = [
    "CAPABILITY_DAEMON",
    "CAPABILITY_DISCORD",
    "CAPABILITY_MANAGED",
    "CAPABILITY_ROTATE",
    "DEFAULT_SCOPES",
    "DESTRUCTIVE_SCOPES",
    "EXIT_CODES",
    "KIND_INSTALL_RECORD",
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
    "LifecycleMode",
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
    "RemovalAction",
    "RemovalHandler",
    "RemovalItem",
    "RemovalOutcome",
    "RemovalScope",
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
    "UninstallPlan",
    "UninstallResult",
    "UpgradeDecision",
    "UpgradeRecord",
    "WizardChoices",
    "api_base_url",
    "assert_secret_free",
    "build_registry",
    "capabilities_for",
    "complete_upgrade",
    "data_locations",
    "default_handlers",
    "default_registry",
    "default_state_path",
    "describe_host",
    "detect_platform",
    "evaluate_support",
    "execute_uninstall",
    "exit_code",
    "http_status",
    "inspect_dashboard",
    "load_state",
    "login_step",
    "login_steps",
    "macos_steps",
    "wsl_steps",
    "onboarding_steps",
    "plan_uninstall",
    "plan_upgrade",
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
    "strip_marked_block",
    "summarize",
]


def __getattr__(name: str):
    """Load the public installer surface only when an export is requested.

    Leaf adapters can be imported without initializing unrelated installers or
    daemon services. The existing re-exports retain their original identities.
    """
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from .command import (
        CommandOutput as CommandOutput,
        CommandRunner as CommandRunner,
        run_command as run_command,
    )
    from .engine import (
        ConsentCallback as ConsentCallback,
        InstallEngine as InstallEngine,
        InstallOptions as InstallOptions,
        ProgressCallback as ProgressCallback,
        ProgressEvent as ProgressEvent,
        run_install as run_install,
    )
    from .lifecycle import (
        DEFAULT_SCOPES as DEFAULT_SCOPES,
        DESTRUCTIVE_SCOPES as DESTRUCTIVE_SCOPES,
        KIND_INSTALL_RECORD as KIND_INSTALL_RECORD,
        LifecycleMode as LifecycleMode,
        RemovalAction as RemovalAction,
        RemovalHandler as RemovalHandler,
        RemovalItem as RemovalItem,
        RemovalOutcome as RemovalOutcome,
        RemovalScope as RemovalScope,
        UninstallPlan as UninstallPlan,
        UninstallResult as UninstallResult,
        UpgradeDecision as UpgradeDecision,
        UpgradeRecord as UpgradeRecord,
        complete_upgrade as complete_upgrade,
        default_handlers as default_handlers,
        execute_uninstall as execute_uninstall,
        plan_uninstall as plan_uninstall,
        plan_upgrade as plan_upgrade,
        strip_marked_block as strip_marked_block,
    )
    from .logins import (
        AuthProbe as AuthProbe,
        CredentialStore as CredentialStore,
        EnvironmentCredential as EnvironmentCredential,
        ProviderLogin as ProviderLogin,
        login_step as login_step,
        login_steps as login_steps,
        probe_all as probe_all,
        probe_login as probe_login,
        provider_logins as provider_logins,
    )
    from .macos import macos_steps as macos_steps
    from .wsl import wsl_steps as wsl_steps
    from .onboarding import (
        CAPABILITY_DAEMON as CAPABILITY_DAEMON,
        CAPABILITY_DISCORD as CAPABILITY_DISCORD,
        DashboardInfo as DashboardInfo,
        HttpProbe as HttpProbe,
        Location as Location,
        api_base_url as api_base_url,
        data_locations as data_locations,
        http_status as http_status,
        inspect_dashboard as inspect_dashboard,
        onboarding_steps as onboarding_steps,
    )
    from .platform import (
        PlatformFacts as PlatformFacts,
        SupportVerdict as SupportVerdict,
        describe_host as describe_host,
        detect_platform as detect_platform,
        evaluate_support as evaluate_support,
    )
    from .postgres import (
        MINIMUM_SERVER_VERSION as MINIMUM_SERVER_VERSION,
        ConnectionCheck as ConnectionCheck,
        PostgresSettings as PostgresSettings,
        PostgresSettingsError as PostgresSettingsError,
    )
    from .postgres_steps import (
        CAPABILITY_MANAGED as CAPABILITY_MANAGED,
        CAPABILITY_ROTATE as CAPABILITY_ROTATE,
        PostgresAdapter as PostgresAdapter,
        postgres_steps as postgres_steps,
    )
    from .prerequisites import (
        default_registry as default_registry,
        prerequisite_steps as prerequisite_steps,
    )
    from .providers import (
        ExecutableProbe as ExecutableProbe,
        ProviderInstaller as ProviderInstaller,
        probe_executable as probe_executable,
        provider_installers as provider_installers,
        provider_step as provider_step,
        provider_steps as provider_steps,
    )
    from .redaction import (
        SecretLeakError as SecretLeakError,
        assert_secret_free as assert_secret_free,
        redact as redact,
    )
    from .registry import build_registry as build_registry, platform_steps as platform_steps
    from .results import (
        EXIT_CODES as EXIT_CODES,
        RESULT_SCHEMA_VERSION as RESULT_SCHEMA_VERSION,
        InstallOutcome as InstallOutcome,
        InstallResult as InstallResult,
        PlanAction as PlanAction,
        PlannedStep as PlannedStep,
        ResourceRecord as ResourceRecord,
        StepResult as StepResult,
        StepState as StepState,
        exit_code as exit_code,
    )
    from .state import (
        STATE_SCHEMA_VERSION as STATE_SCHEMA_VERSION,
        IncompatibleStateError as IncompatibleStateError,
        InstallState as InstallState,
        StateError as StateError,
        StepRecord as StepRecord,
        default_state_path as default_state_path,
        load_state as load_state,
        save_state as save_state,
    )
    from .steps import (
        InstallPlanError as InstallPlanError,
        StepContext as StepContext,
        StepRegistry as StepRegistry,
        StepSpec as StepSpec,
    )
    from .wizard import (
        OnboardingSummary as OnboardingSummary,
        Question as Question,
        WizardChoices as WizardChoices,
        capabilities_for as capabilities_for,
        question_plan as question_plan,
        summarize as summarize,
    )

    exported = locals()
    globals().update({export: exported[export] for export in __all__})
    return globals()[name]


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
