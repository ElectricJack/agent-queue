"""Guided provider login and non-secret authentication readiness.

``noble-apex.7`` made a harness *installed*.  This module makes the second
half of that distinction observable: a harness is **authenticated** only when
the provider's own credential store or a provider-supported environment
credential says so.  The contract's "Human authentication checkpoints" table
is what these adapters implement.

Three rules shape everything here, and each is enforced mechanically rather
than by convention:

**AQ never enters a credential.**  There is no password prompt, no token
capture and no browser automation.  A step that finds no credential returns
``needs_user`` with the provider's *own* documented command, and the human
runs it.  Retrying is just rerunning ``aq install``.

**The probe reads no credential material.**  Readiness is decided by, in
order: the provider's own non-secret status command (exit status only — its
output is never captured into a result), the *names* of environment variables
that are set (never their values), and the *existence* of the provider's
credential file (never its contents).  That is why a probe can report
``authenticated`` for a store AQ is not allowed to open.

**Nothing durable learns a secret.**  Results carry ``auth_method``,
``credential_source`` and ``credential_store`` — names and states, never
material — which is exactly the vocabulary :mod:`src.install.redaction`
allowlists, so the resume record's secret fence stays armed for everything
else.

Provider facts (commands, variables, store paths) were re-verified against the
official documentation linked on each :class:`ProviderLogin` while this module
was written; they live here as data so a provider change is an edit to one
tuple, not to the engine.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .providers import CLAUDE_CODE, CODEX, GEMINI, ProviderInstaller, user_bin_aware_which
from .results import StepResult
from .steps import StepContext, StepSpec

CommandRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
CommandLookup = Callable[[str], str | None]

#: A status command that has not answered in this long is treated as "cannot
#: tell", never as authenticated.  A hung CLI must not hang ``aq install``, but
#: the first run of a freshly installed or just-updated CLI can be slow -- a
#: macOS Gatekeeper scan, a self-update check -- and ten seconds reported a
#: signed-in harness as signed out on exactly the one-command install path.
STATUS_TIMEOUT_SECONDS = 30

#: Values that turn a switch-shaped variable (``GOOGLE_GENAI_USE_VERTEXAI``)
#: off.  ``=false`` means the operator did *not* select that method.
_FALSEY = frozenset({"", "0", "false", "no", "off"})


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        # A status probe must never read the terminal.  `aq install` holds the
        # user's tty for its own prompts, and a child that waited on it would
        # turn a readiness check into a hang or a stolen keystroke.
        stdin=subprocess.DEVNULL,
        text=True,
        timeout=STATUS_TIMEOUT_SECONDS,
    )


@dataclass(frozen=True, slots=True)
class EnvironmentCredential:
    """One provider-supported environment credential, identified by name.

    ``companions`` are the variables the method additionally requires (Vertex
    AI needs a project and a location).  When the selector is set and a
    companion is missing, the probe reports the *missing names* rather than
    silently treating the method as unconfigured — an operator who set two of
    three variables wants to be told which one is absent.
    """

    variable: str
    method: str
    description: str
    companions: tuple[str, ...] = ()
    #: True when the variable is a switch: only a truthy value selects it.
    switch: bool = False

    def selected(self, environ: Mapping[str, str]) -> bool:
        value = environ.get(self.variable)
        if value is None or not value.strip():
            return False
        # A switch-shaped variable set to ``false`` selects nothing.
        return not (self.switch and value.strip().lower() in _FALSEY)

    def missing_companions(self, environ: Mapping[str, str]) -> tuple[str, ...]:
        return tuple(name for name in self.companions if not (environ.get(name) or "").strip())


def _config_path(
    environ: Mapping[str, str], *, filename: str, default_dir: str, directory_variable: str | None
) -> Path:
    """Resolve a provider config file the way the provider does."""
    override = (environ.get(directory_variable) or "").strip() if directory_variable else ""
    if override:
        return Path(override).expanduser() / filename
    home = (environ.get("HOME") or "").strip()
    root = Path(home) if home else Path.home()
    return root / default_dir / filename


@dataclass(frozen=True, slots=True)
class SettingsEnvironment:
    """A provider settings file whose ``env`` block the provider applies itself.

    Claude Code documents ``env`` in ``~/.claude/settings.json`` as variables
    applied to every session, and it is the usual home for a cloud-provider
    selection such as ``CLAUDE_CODE_USE_VERTEX``.  Those variables never reach
    this process's environment, so a probe that looked only at ``os.environ``
    called a Vertex-configured harness unauthenticated.

    Only the *names* and switch values are consulted, exactly as for a real
    environment variable; nothing read here is recorded.
    """

    label: str
    filename: str
    default_dir: str
    directory_variable: str | None = None

    def resolve(self, environ: Mapping[str, str]) -> Path:
        return _config_path(
            environ,
            filename=self.filename,
            default_dir=self.default_dir,
            directory_variable=self.directory_variable,
        )

    def load(self, environ: Mapping[str, str]) -> dict[str, str]:
        """The file's ``env`` block, or nothing -- a bad file is no evidence."""
        try:
            payload = json.loads(self.resolve(environ).read_text())
        except (OSError, ValueError):
            return {}
        block = payload.get("env") if isinstance(payload, dict) else None
        if not isinstance(block, dict):
            return {}
        return {
            str(name): str(value).lower() if isinstance(value, bool) else str(value)
            for name, value in block.items()
            if isinstance(value, (str, int, float, bool))
        }


@dataclass(frozen=True, slots=True)
class CredentialStore:
    """A provider-owned protected store, checked for existence only.

    AQ neither creates, reads, moves nor deletes one of these: it is the
    provider's file, written by the provider's login flow, and the installer's
    interest in it stops at "is there one?".
    """

    label: str
    filename: str
    #: Directory relative to the user's home when no override variable is set.
    default_dir: str
    directory_variable: str | None = None
    #: True when the real store is an OS keychain this probe cannot inspect,
    #: and the file below is only the documented fallback location.
    keychain_backed: bool = False

    def resolve(self, environ: Mapping[str, str]) -> Path:
        return _config_path(
            environ,
            filename=self.filename,
            default_dir=self.default_dir,
            directory_variable=self.directory_variable,
        )

    def present(self, environ: Mapping[str, str]) -> bool:
        try:
            return self.resolve(environ).exists()
        except OSError:  # pragma: no cover - an unreadable home is "no store"
            return False


@dataclass(frozen=True, slots=True)
class ProviderLogin:
    """How one harness is logged in to, and how readiness is observed."""

    provider_id: str
    title: str
    executable: str
    #: The provider's own interactive login command, quoted to the human
    #: verbatim.  AQ prints it; the human runs it.
    login_command: str
    #: A non-secret status command whose *exit status* answers "signed in?".
    #: ``None`` when the provider documents none — then the store and the
    #: environment are the only evidence.
    status_command: tuple[str, ...] | None
    environment: tuple[EnvironmentCredential, ...]
    stores: tuple[CredentialStore, ...]
    #: What to do when this host has no browser: the provider's documented
    #: device-code or environment-credential route.
    headless_hint: str
    docs_url: str
    #: Extra sentence appended to a "not authenticated" report when the
    #: provider's real store is not inspectable on this platform.
    store_note: str = ""
    #: Settings files whose ``env`` block the provider applies to itself, read
    #: as environment credentials the process environment does not carry.
    settings: tuple[SettingsEnvironment, ...] = ()

    @property
    def capability(self) -> str:
        return f"provider.{self.provider_id}"

    @property
    def install_step_id(self) -> str:
        return f"provider.{self.provider_id}-cli"

    @property
    def step_id(self) -> str:
        return f"provider.{self.provider_id}-login"


@dataclass(frozen=True, slots=True)
class AuthProbe:
    """What a readiness probe observed — names and states, never material."""

    provider_id: str
    installed: bool
    authenticated: bool
    method: str | None = None
    source: str | None = None
    store: str | None = None
    #: Everything the probe looked at, so a human can see why the answer is
    #: what it is (command names, variable names, store labels).
    checked: tuple[str, ...] = ()
    #: Variables a selected method still needs before it can work.
    missing_environment: tuple[str, ...] = ()

    def detail(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "authenticated": self.authenticated,
            "auth_method": self.method,
            "credential_source": self.source,
            "credential_store": self.store,
            "checked": list(self.checked),
            "missing_environment": list(self.missing_environment),
        }


def _status_command_says_signed_in(command: Sequence[str], runner: CommandRunner) -> bool:
    """Run a provider status command and read only its exit status.

    The output is deliberately dropped: it can name an account, a plan or an
    organisation, none of which the installer has any business persisting.
    """
    # One retry when the command could not answer at all: a slow first run
    # is common and transient, and the one-command install should not stop
    # the whole machine setup on it.  A command that *answered* "no" is not
    # retried.
    for _attempt in range(2):
        try:
            completed = runner(tuple(command))
        except (OSError, subprocess.SubprocessError):
            # A missing subcommand, a hung CLI or an old build is "cannot
            # tell", and the caller falls through to the environment and store.
            continue
        return completed.returncode == 0
    return False


def probe_login(
    login: ProviderLogin,
    *,
    environ: Mapping[str, str] | None = None,
    which: CommandLookup = shutil.which,
    runner: CommandRunner = _run,
) -> AuthProbe:
    """Decide whether *login*'s harness is installed and authenticated.

    Evidence is taken in the provider's own order of authority: its status
    command, then a supported environment credential, then the presence of its
    credential store.  Each source contributes a name; none contributes a
    value.
    """
    process_env = os.environ if environ is None else environ
    executable = which(login.executable)
    if executable is None:
        return AuthProbe(login.provider_id, installed=False, authenticated=False)

    # The provider applies its settings-file `env` block to every session, on
    # top of what it inherits, so evaluate credentials the same way.
    from_settings: dict[str, str] = {}
    settings_label: dict[str, str] = {}
    for settings in login.settings:
        for name, value in settings.load(process_env).items():
            from_settings[name] = value
            settings_label[name] = settings.label
    env: Mapping[str, str] = {**process_env, **from_settings} if from_settings else process_env

    checked: list[str] = []
    missing: list[str] = []

    if login.status_command:
        rendered = " ".join(login.status_command)
        checked.append(rendered)
        # Ask the executable this probe actually found.  The status command names
        # the CLI by its bare name, which does not run when the CLI lives
        # somewhere this process's PATH does not cover (~/.local/bin, a Homebrew
        # prefix): the command then "could not answer", and a signed-in harness
        # was reported as unauthenticated.
        command = (executable, *login.status_command[1:])
        if _status_command_says_signed_in(command, runner):
            return AuthProbe(
                login.provider_id,
                installed=True,
                authenticated=True,
                method="provider-login",
                source=rendered,
                store=login.stores[0].label if login.stores else None,
                checked=tuple(checked),
            )

    checked.extend(settings.label for settings in login.settings)
    for credential in login.environment:
        checked.append(credential.variable)
        if not credential.selected(env):
            continue
        absent = credential.missing_companions(env)
        if absent:
            missing.extend(name for name in absent if name not in missing)
            continue
        return AuthProbe(
            login.provider_id,
            installed=True,
            authenticated=True,
            method=credential.method,
            source=credential.variable,
            store=settings_label.get(credential.variable, "environment"),
            checked=tuple(checked),
        )

    for store in login.stores:
        checked.append(store.label)
        if store.present(env):
            return AuthProbe(
                login.provider_id,
                installed=True,
                authenticated=True,
                method="provider-login",
                source=store.label,
                store=store.label,
                checked=tuple(checked),
            )

    return AuthProbe(
        login.provider_id,
        installed=True,
        authenticated=False,
        checked=tuple(checked),
        missing_environment=tuple(missing),
    )


def login_instructions(login: ProviderLogin, probe: AuthProbe, *, interactive: bool) -> str:
    """The remediation for a harness that is installed but not authenticated.

    Interactive and unattended runs get genuinely different instructions: the
    first can send a human to a browser, the second must not read a terminal
    or open one, so it is told which environment credential to supply.
    """
    parts: list[str] = []
    if probe.missing_environment:
        parts.append(
            "Set "
            + ", ".join(probe.missing_environment)
            + f" to finish the {login.title} configuration this environment already selected."
        )
    if interactive:
        parts.append(
            f"Run `{login.login_command}` yourself and complete the {login.title} sign-in — "
            "AQ never types a password, token or browser consent for you. Then rerun "
            f"`aq install --with {login.capability}`; it re-checks and continues."
        )
        parts.append(f"No browser on this host? {login.headless_hint}")
    else:
        parts.append(
            f"This unattended run may not open a browser or read a terminal. {login.headless_hint} "
            f"Alternatively run `{login.login_command}` once in an interactive shell on this "
            f"host, then rerun `aq install --with {login.capability}`."
        )
    if login.store_note:
        parts.append(login.store_note)
    parts.append(f"Provider documentation: {login.docs_url}")
    return " ".join(parts)


def login_step(
    login: ProviderLogin,
    *,
    environ: Mapping[str, str] | None = None,
    which: CommandLookup = shutil.which,
    runner: CommandRunner = _run,
) -> StepSpec:
    """Build the authentication checkpoint step for *login*.

    The step is read-only, so it needs no consent and runs in a dry run: it
    reports the checkpoint rather than crossing it.
    """

    def observe() -> AuthProbe:
        return probe_login(login, environ=environ, which=which, runner=runner)

    def run(context: StepContext) -> StepResult:
        probe = observe()
        if not probe.installed:
            return StepResult.failed(
                login.step_id,
                f"{login.executable} is not on PATH, so its authentication cannot be checked",
                (
                    f"Install the CLI first: `aq install --with {login.capability} "
                    f"--restart-from {login.install_step_id}`."
                ),
                detail=probe.detail(),
            )
        if probe.authenticated:
            return StepResult.succeeded(
                login.step_id,
                f"{login.title} is authenticated ({probe.method} via {probe.source})",
                detail=probe.detail(),
            )
        interactive = getattr(context, "interactive", True)
        return StepResult.needs_user(
            login.step_id,
            f"{login.title} is installed but not authenticated",
            login_instructions(login, probe, interactive=interactive),
            detail=probe.detail(),
        )

    return StepSpec(
        id=login.step_id,
        title=f"Authenticate {login.title}",
        description=(
            f"Distinguishes an installed {login.executable} from an authenticated one using "
            "the provider's own status command, supported environment credentials and the "
            "presence of its credential store — never their contents."
        ),
        run=run,
        depends_on=(login.install_step_id,),
        capability=login.capability,
        # Signing in is a human action in a browser, and nothing later in the
        # install depends on it: writing the configuration, starting the daemon
        # and building the dashboard are all independent.  Halting here made one
        # unauthenticated harness cost the operator their whole machine setup and
        # a second `aq install`; the run now finishes and still reports
        # needs_user, with the sign-in as the next action.
        halts=False,
        verify=lambda context: observe().authenticated,
        owner="provider",
    )


CLAUDE_CODE_LOGIN = ProviderLogin(
    provider_id=CLAUDE_CODE.id,
    title=CLAUDE_CODE.title,
    executable=CLAUDE_CODE.executable,
    login_command="claude auth login",
    status_command=("claude", "auth", "status"),
    environment=(
        # Documented precedence order: a cloud provider selection outranks a
        # bearer token, which outranks an API key, which outranks a
        # `setup-token` OAuth token.
        EnvironmentCredential(
            "CLAUDE_CODE_USE_BEDROCK",
            "cloud-provider",
            "Amazon Bedrock credentials supplied by the AWS credential chain",
            switch=True,
        ),
        EnvironmentCredential(
            "CLAUDE_CODE_USE_VERTEX",
            "cloud-provider",
            "Google Cloud credentials supplied by the gcloud credential chain",
            switch=True,
        ),
        EnvironmentCredential(
            "CLAUDE_CODE_USE_FOUNDRY",
            "cloud-provider",
            "Microsoft Foundry credentials supplied by the Azure credential chain",
            switch=True,
        ),
        EnvironmentCredential(
            "ANTHROPIC_AUTH_TOKEN",
            "auth-token",
            "Bearer token for an LLM gateway or proxy",
        ),
        EnvironmentCredential(
            "ANTHROPIC_API_KEY",
            "api-key",
            "Claude Console API key",
        ),
        EnvironmentCredential(
            "CLAUDE_CODE_OAUTH_TOKEN",
            "oauth-token",
            "Long-lived OAuth token from `claude setup-token`",
        ),
    ),
    stores=(
        CredentialStore(
            label="Claude Code credential file",
            filename=".credentials.json",
            default_dir=".claude",
            directory_variable="CLAUDE_CONFIG_DIR",
            keychain_backed=True,
        ),
    ),
    settings=(
        SettingsEnvironment(
            label="Claude Code settings.json env",
            filename="settings.json",
            default_dir=".claude",
            directory_variable="CLAUDE_CONFIG_DIR",
        ),
    ),
    headless_hint=(
        "Run `claude setup-token` on a machine that has a browser and export the printed "
        "token here as CLAUDE_CODE_OAUTH_TOKEN, or export ANTHROPIC_API_KEY from a Claude "
        "Console key."
    ),
    store_note=(
        "On macOS the login lives in the encrypted Keychain, which AQ does not read; "
        "`claude auth status` is the readiness signal there."
    ),
    docs_url="https://code.claude.com/docs/en/authentication",
)

CODEX_LOGIN = ProviderLogin(
    provider_id=CODEX.id,
    title=CODEX.title,
    executable=CODEX.executable,
    login_command="codex login",
    status_command=("codex", "login", "status"),
    environment=(
        EnvironmentCredential(
            "OPENAI_API_KEY",
            "api-key",
            "OpenAI API key, as used by `codex login --with-api-key`",
        ),
    ),
    stores=(
        CredentialStore(
            label="Codex credential file",
            filename="auth.json",
            default_dir=".codex",
            directory_variable="CODEX_HOME",
        ),
    ),
    headless_hint=(
        "Run `codex login --device-auth` and enter the one-time code in a browser on another "
        "device, or pipe a key in with `printenv OPENAI_API_KEY | codex login --with-api-key`."
    ),
    docs_url="https://developers.openai.com/codex/auth",
)

GEMINI_LOGIN = ProviderLogin(
    provider_id=GEMINI.id,
    title=GEMINI.title,
    executable=GEMINI.executable,
    # Gemini documents no non-interactive status subcommand, so the login is
    # the interactive `/auth` dialog and readiness comes from the environment
    # or the OAuth cache's existence.
    login_command="gemini (then /auth)",
    status_command=None,
    environment=(
        EnvironmentCredential(
            "GEMINI_API_KEY",
            "api-key",
            "Google AI Studio API key",
        ),
        EnvironmentCredential(
            "GOOGLE_API_KEY",
            "api-key",
            "Google Cloud API key",
        ),
        EnvironmentCredential(
            "GOOGLE_GENAI_USE_VERTEXAI",
            "vertex",
            "Vertex AI, with a project and a location",
            companions=("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"),
            switch=True,
        ),
        EnvironmentCredential(
            "GOOGLE_APPLICATION_CREDENTIALS",
            "service-account",
            "Path to a Google service-account key file",
        ),
    ),
    stores=(
        CredentialStore(
            label="Gemini CLI OAuth cache",
            filename="oauth_creds.json",
            default_dir=".gemini",
        ),
    ),
    headless_hint=(
        "Gemini's Google login needs a browser on this host. Instead export GEMINI_API_KEY, "
        "or set GOOGLE_GENAI_USE_VERTEXAI=true with GOOGLE_CLOUD_PROJECT and "
        "GOOGLE_CLOUD_LOCATION, or point GOOGLE_APPLICATION_CREDENTIALS at a service-account "
        "key file."
    ),
    docs_url="https://google-gemini.github.io/gemini-cli/docs/get-started/authentication.html",
)


def provider_logins(
    additional: Iterable[ProviderLogin] = (),
) -> tuple[ProviderLogin, ...]:
    """Return AQ's login adapters plus caller-supplied ones.

    Duplicate provider ids are rejected here for the same reason
    :func:`src.install.providers.provider_installers` rejects them: two
    adapters claiming one capability would make the selected step ambiguous.
    """
    logins = (CLAUDE_CODE_LOGIN, CODEX_LOGIN, GEMINI_LOGIN, *additional)
    ids = [login.provider_id for login in logins]
    if len(ids) != len(set(ids)):
        raise ValueError("provider login ids must be unique")
    return logins


def login_steps(
    *,
    environ: Mapping[str, str] | None = None,
    which: CommandLookup = shutil.which,
    runner: CommandRunner = _run,
    additional: Iterable[ProviderLogin] = (),
    installers: Sequence[ProviderInstaller] | None = None,
) -> tuple[StepSpec, ...]:
    """Build a login step for every provider that also has an install step.

    *installers* is the registry the steps must attach to.  A login adapter
    for a provider nobody installs would name a dependency that does not
    exist, and :meth:`StepRegistry.ordered` would refuse the whole plan — so
    the unmatched adapter is dropped rather than allowed to break the install.
    """
    known = {installer.id for installer in installers} if installers is not None else None
    return tuple(
        login_step(login, environ=environ, which=which, runner=runner)
        for login in provider_logins(additional)
        if known is None or login.provider_id in known
    )


def probe_all(
    *,
    environ: Mapping[str, str] | None = None,
    which: CommandLookup | None = None,
    runner: CommandRunner = _run,
    additional: Iterable[ProviderLogin] = (),
) -> tuple[AuthProbe, ...]:
    """Probe every provider independently, for a readiness summary.

    The engine stops at the first unsatisfied step, which is right for an
    install but wrong for a report: the readiness definition wants *each*
    selected harness marked authenticated or needs-user in one pass.  This is
    that pass, and it mutates nothing.
    """
    # The same lookup the install steps use, for the same reason: a provider CLI
    # its own installer put in ~/.local/bin is installed, and a readiness report
    # that says "Claude Code is not installed" right after installing it sends
    # the operator after the wrong thing -- and leaves its worker profiles
    # inactive, so no pool is created for it.
    lookup = which or user_bin_aware_which()
    return tuple(
        probe_login(login, environ=environ, which=lookup, runner=runner)
        for login in provider_logins(additional)
    )


__all__ = [
    "CLAUDE_CODE_LOGIN",
    "CODEX_LOGIN",
    "GEMINI_LOGIN",
    "STATUS_TIMEOUT_SECONDS",
    "SettingsEnvironment",
    "AuthProbe",
    "CredentialStore",
    "EnvironmentCredential",
    "ProviderLogin",
    "login_instructions",
    "login_step",
    "login_steps",
    "probe_all",
    "probe_login",
    "provider_logins",
]
