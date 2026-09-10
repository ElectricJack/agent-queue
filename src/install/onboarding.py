"""The onboarding tail of ``aq install``: configuration, messaging, daemon, dashboard.

The prerequisite, PostgreSQL and provider adapters stop at "this machine has
what AQ needs".  A newcomer needs one step further: a configuration file with
defaults that suit *this* box, a daemon that answers, and a sentence telling
them where their data lives and which URL to open.  Those are the steps here,
and they are ordinary :class:`~src.install.steps.StepSpec` values, so they
inherit the engine's consent, resume and machine-readable reporting instead of
re-implementing an onboarding flow beside it.

Three boundaries are deliberate:

* **Discord is optional.**  It is a capability, so an install that never
  mentions it records ``skipped`` and finishes ``ready``.  Selecting it never
  puts a token in the configuration, the result or the resume record — the
  step writes the *name* of the environment variable and stops at
  ``needs_user`` until the human has put the value in the ``.env`` file.
* **Schema work is not ours.**  ``config.check`` reads the configuration and
  reports what it names; creating or migrating tables stays with the
  daemon/operator path (``docs/guides/migrations.md``).
* **Every reader is injectable.**  A clock, an HTTP probe and a command runner
  are parameters with real defaults, so the whole flow is provable on a box
  with no daemon, no database and no network.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .command import CommandOutput, CommandRunner, run_command
from .postgres import CONFIG_HEADER, backup_path_for, read_env_file, split_password
from .prerequisites import STEP_DATA_DIR
from .redaction import redact
from .results import RESOURCE_CONFIG, ResourceRecord, StepResult
from .state import DEFAULT_STATE_FILENAME, default_state_dir
from .steps import StepContext, StepSpec

#: Optional capabilities this module registers.  Both are opt-in: an install
#: that selects neither still reaches ``ready``.
CAPABILITY_DISCORD = "discord"
CAPABILITY_DAEMON = "daemon"

STEP_CONFIG = "config.defaults"
STEP_CHECK = "config.check"
STEP_DISCORD = "config.discord"
STEP_DAEMON = "daemon.start"
STEP_DASHBOARD = "daemon.dashboard"

#: Where the daemon serves the packaged dashboard, relative to its API base.
DASHBOARD_PATH = "/dashboard"

#: The Vite port a source checkout serves the dashboard from.
SOURCE_DASHBOARD_URL = "http://localhost:5173"

#: Defaults matching :func:`src.cli.client._resolve_api_url`, which is what
#: every other ``aq`` command uses to find the daemon.
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8081

#: Seconds ``aq start`` may take.  It waits for ``/health`` itself.
DAEMON_START_TIMEOUT = 180.0

#: Where the daemon puts worker checkouts when the configuration does not say.
#: Mirrors ``AppConfig.workspace_dir``'s default in :mod:`src.config`.
DEFAULT_WORKSPACE_DIR = os.path.expanduser("~/agent-queue-workspaces")

_DISCORD_TOKEN_ENV = "DISCORD_BOT_TOKEN"

#: Keys accepted under ``settings.discord`` in the install input file.
_DISCORD_SETTING_KEYS = frozenset({"channel_id", "guild_id", "credential_variable"})


# ---------------------------------------------------------------------------
# Where things live
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Location:
    """One place AQ keeps something, and what it keeps there."""

    label: str
    path: str
    note: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "path": self.path, "note": self.note}


def data_locations(home: Path, config: Mapping[str, Any] | None = None) -> tuple[Location, ...]:
    """Every path a newcomer is entitled to know about, derived from *home*.

    This is the answer to "where does AQ put my things?", and it is computed
    rather than prose so the installer's summary and the documentation cannot
    drift.  It is also secret-free: the database appears as a host, a port and
    a name, never as a DSN with a password in it.
    """
    raw = dict(config or {})
    # The fallback is AppConfig's own default, not a guess: a summary that
    # named a directory the daemon does not use would send someone looking for
    # their worktrees in the wrong place.
    workspace = str(raw.get("workspace_dir") or DEFAULT_WORKSPACE_DIR)
    locations = [
        Location(
            "Configuration",
            str(home / "config.yaml"),
            "settings, tuned for this machine; edit with `aq system config edit`",
        ),
        Location(
            "Secrets",
            str(home / ".env"),
            "environment values the configuration refers to as ${VAR}, mode 0600",
        ),
        Location(
            "Vault",
            str(home / "vault"),
            "playbooks, agent profiles, memory and facts as markdown",
        ),
        Location(
            "Worktrees",
            workspace,
            "each worker's isolated checkout of a project repository",
        ),
        Location(
            "Daemon log",
            str(home / "daemon.log"),
            "what the running daemon wrote; `aq logs` reads it",
        ),
        Location(
            "Resume record",
            str(home / DEFAULT_STATE_FILENAME),
            "what `aq install` has completed and what it owns; never a credential",
        ),
    ]
    database = raw.get("database")
    url = database.get("url") if isinstance(database, Mapping) else None
    if isinstance(url, str) and url:
        safe, _password = split_password(url)
        locations.append(
            Location(
                "Database",
                safe,
                "tasks, projects, sessions and results; AQ never stores them in files",
            )
        )
    return tuple(locations)


def config_path_for(environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    return (home or default_state_dir(environ)) / "config.yaml"


def api_base_url(config: Mapping[str, Any] | None, environ: Mapping[str, str] | None = None) -> str:
    """The daemon's API base, resolved the way every other ``aq`` command does.

    ``AQ_API_URL`` wins over the configuration because a session that was
    handed one is talking to a daemon somewhere else, and an installer that
    ignored it would report the wrong URL to the human.
    """
    environ = os.environ if environ is None else environ
    override = (environ.get("AQ_API_URL") or environ.get("AGENT_QUEUE_API_URL") or "").strip()
    if override:
        return override.rstrip("/")
    section = (config or {}).get("mcp_server")
    host = DEFAULT_API_HOST
    port: Any = DEFAULT_API_PORT
    if isinstance(section, Mapping):
        host = str(section.get("host") or DEFAULT_API_HOST)
        port = section.get("port") or DEFAULT_API_PORT
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = DEFAULT_API_PORT
    return f"http://{host}:{port}"


HttpProbe = Callable[[str], int | None]


def http_status(url: str, timeout: float = 2.0) -> int | None:
    """The status code *url* answers with, or ``None`` when nothing answers.

    Only the code is read.  The installer has no business capturing a response
    body from a service it is merely checking for a pulse.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        code = int(error.code)
        error.close()
        return code
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _read_config(path: Path) -> dict[str, Any]:
    """The configuration as written, or ``{}`` when it is absent or unreadable."""
    if not path.exists():
        return {}
    try:
        from src.config_editor import read_raw_config

        return read_raw_config(str(path))
    except Exception:  # noqa: BLE001 - a broken config is reported by config.check
        return {}


# ---------------------------------------------------------------------------
# config.defaults — a configuration file tuned for this machine
# ---------------------------------------------------------------------------


def config_step(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    depends_on: tuple[str, ...] = (STEP_DATA_DIR,),
) -> StepSpec:
    """Create ``config.yaml`` when it is absent and give it resource-aware defaults.

    The defaults come from :mod:`src.config_tuning`, which derives them from
    the box's cores and RAM — the same values ``aq system config tune --apply``
    writes.  A section the operator has already written is *kept*: this step
    fills gaps, it does not overwrite an opinion.
    """
    path = config_path_for(environ, home)

    def _plan_is_complete() -> bool:
        from src.config_tuning import MachineResources, tuning_plan

        if not path.exists():
            return False
        try:
            current = _read_config(path)
            plans = tuning_plan(current, MachineResources.detect())
        except Exception:  # noqa: BLE001 - an unreadable config is not "satisfied"
            return False
        return not any(plan.action == "add" for plan in plans)

    def run(context: StepContext) -> StepResult:
        from src.config_tuning import MachineResources, apply_tuning, tuning_plan

        created = not path.exists()
        if created:
            path.parent.mkdir(parents=True, exist_ok=True)
            # The header selects no messaging platform: a machine that never
            # configures Discord must still produce a configuration the daemon
            # will load.
            path.write_text(CONFIG_HEADER, encoding="utf-8")

        machine = MachineResources.detect()
        # The contract asks for a versioned backup *before* AQ's configuration
        # is changed — and only then.  A run that has nothing to add must not
        # leave a numbered copy behind on every rerun.
        if not created and any(
            plan.action in ("add", "replace") for plan in tuning_plan(_read_config(path), machine)
        ):
            backup_path_for(path).write_bytes(path.read_bytes())

        try:
            outcome = apply_tuning(str(path), machine)
        except Exception as error:  # noqa: BLE001 - untuned defaults still run
            return StepResult.succeeded(
                STEP_CONFIG,
                f"{path.name} is in place; default tuning could not be written ({error})",
                detail={"config_path": str(path), "created": created, "tuned": False},
                resources=(
                    ResourceRecord(
                        kind=RESOURCE_CONFIG, id=str(path), owned=created, reused=not created
                    ),
                ),
            )
        if outcome.get("validation_errors"):
            return StepResult.failed(
                STEP_CONFIG,
                "the tuned defaults do not validate: "
                + "; ".join(str(error) for error in outcome["validation_errors"]),
                "Fix the configuration keys named above (or delete "
                f"{path} to start from AQ's defaults) and rerun `aq install`.",
                detail={"config_path": str(path), "created": created},
            )
        written = list(outcome.get("written") or [])
        summary = (
            f"{'created' if created else 'using'} {path} — "
            f"{machine.cores} cores, {machine.memory_gb:.0f} GiB ({machine.size_class}): "
            f"{machine.concurrent_agents} concurrent agent(s), {machine.test_slots} test slot(s)"
        )
        return StepResult.succeeded(
            STEP_CONFIG,
            summary,
            detail={
                "config_path": str(path),
                "created": created,
                "tuned": True,
                "written": written,
                "kept": list(outcome.get("kept") or []),
                "machine": machine.as_dict(),
                "rationale": "docs/guides/default-tuning.md",
            },
            resources=(
                ResourceRecord(
                    kind=RESOURCE_CONFIG, id=str(path), owned=created, reused=not created
                ),
            ),
        )

    return StepSpec(
        id=STEP_CONFIG,
        title="Write configuration defaults for this machine",
        description=(
            f"Creates {path} when it is absent and adds resource-aware defaults derived from "
            "this box's cores and memory. Sections you have already written are kept."
        ),
        run=run,
        depends_on=depends_on,
        mutating=True,
        consent_prompt=f"Write AQ's default settings to {path}?",
        verify=lambda context: _plan_is_complete(),
        owner="onboarding",
    )


# ---------------------------------------------------------------------------
# config.check — it parses, its secrets resolve, and here is where things live
# ---------------------------------------------------------------------------


def check_step(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    depends_on: tuple[str, ...] = (STEP_CONFIG,),
) -> StepSpec:
    """Load the configuration the daemon will load, and report where data lives.

    This is readiness condition 2 of the contract — "configuration parses and
    its secret references resolve without displaying secret values" — and it is
    the step that turns the installer's result into an answer to "where did AQ
    put my things?".
    """
    path = config_path_for(environ, home)

    def run(context: StepContext) -> StepResult:
        from src.config import ConfigValidationError, load_config

        raw = _read_config(path)
        locations = data_locations(path.parent, raw)
        detail: dict[str, Any] = {
            "config_path": str(path),
            "locations": [location.to_dict() for location in locations],
            "messaging_platform": str(raw.get("messaging_platform") or "none"),
        }
        if context.dry_run and not path.exists():
            # The step that writes the configuration is mutating, so a dry run
            # did not execute it.  Reporting "it does not parse" here would be
            # a failure the dry run itself caused.
            return StepResult.skipped(
                STEP_CHECK,
                "dry run — the configuration was not written",
                detail=detail,
            )
        try:
            config = load_config(str(path))
        except FileNotFoundError:
            return StepResult.failed(
                STEP_CHECK,
                f"no configuration at {path}",
                f"Rerun `aq install --restart-from {STEP_CONFIG}` to write one.",
                detail=detail,
            )
        except ConfigValidationError as error:
            return StepResult.failed(
                STEP_CHECK,
                redact(f"the configuration does not validate: {error}"),
                (
                    f"Correct the keys named above in {path} (`aq system config edit` opens it), "
                    "then rerun `aq install`. Discord is optional: leave "
                    "`messaging_platform: none` unless you selected it."
                ),
                detail=detail,
            )
        except Exception as error:  # noqa: BLE001 - an unresolved ${VAR} lands here
            return StepResult.failed(
                STEP_CHECK,
                redact(f"the configuration could not be loaded: {error}"),
                (
                    f"Resolve the reference named above — values live in {path.parent / '.env'} "
                    "(mode 0600), never in config.yaml — then rerun `aq install`."
                ),
                detail=detail,
            )
        # Now that it has loaded, report what the daemon will actually use
        # rather than what the file happens to spell out.
        resolved = dict(raw)
        resolved["workspace_dir"] = config.workspace_dir
        detail["locations"] = [
            location.to_dict() for location in data_locations(path.parent, resolved)
        ]
        detail["messaging_platform"] = config.messaging_platform
        detail["api_url"] = api_base_url(raw, environ)
        return StepResult.succeeded(
            STEP_CHECK,
            f"{path} parses and its references resolve",
            detail=detail,
        )

    return StepSpec(
        id=STEP_CHECK,
        title="Check the configuration the daemon will load",
        description=(
            "Loads config.yaml exactly as the daemon does — including ${VAR} references — and "
            "reports where AQ stores configuration, secrets, the vault, worktrees and logs."
        ),
        run=run,
        depends_on=depends_on,
        # The step only reads, so revalidating it *is* running it, and the
        # result it returns is the one the engine records.  A bare boolean
        # would answer "the configuration still parses" and throw away
        # ``locations`` — which is why every rerun, repair and upgrade used to
        # print an empty "Where AQ stores your data".
        verify=run,
        owner="onboarding",
    )


# ---------------------------------------------------------------------------
# config.discord — optional, and never a place a token is stored
# ---------------------------------------------------------------------------


def discord_step(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    depends_on: tuple[str, ...] = (STEP_CHECK,),
) -> StepSpec:
    """Point AQ's digest and escalation delivery at one Discord channel.

    AQ is not controlled through Discord: it delivers an hourly digest and one
    escalation thread per human decision there, and nothing else.  A machine
    that never selects this capability is fully installed without it, which is
    why the step is gated rather than merely skippable.
    """
    path = config_path_for(environ, home)

    def _settings(context: StepContext) -> Mapping[str, Any]:
        section = context.option("discord") or {}
        return section if isinstance(section, Mapping) else {}

    def _token_present(token_env: str) -> bool:
        env = os.environ if environ is None else environ
        if (env.get(token_env) or "").strip():
            return True
        return bool((read_env_file(path.parent / ".env").get(token_env) or "").strip())

    def _configured() -> bool:
        raw = _read_config(path)
        if str(raw.get("messaging_platform") or "") != "discord":
            return False
        section = raw.get("discord")
        return isinstance(section, Mapping) and bool(section.get("channel_id"))

    def run(context: StepContext) -> StepResult:
        from src.config_editor import write_section

        settings = _settings(context)
        unknown = sorted(set(settings) - _DISCORD_SETTING_KEYS)
        if unknown:
            # An unattended run that silently ignored a misspelled setting
            # would deliver to the wrong channel, or to none.
            return StepResult.failed(
                STEP_DISCORD,
                f"unknown discord setting(s): {', '.join(unknown)}",
                "Recognised keys under `settings.discord` are "
                f"{', '.join(sorted(_DISCORD_SETTING_KEYS))}.",
                detail={"unknown": unknown},
                retryable=False,
            )
        token_env = str(settings.get("credential_variable") or _DISCORD_TOKEN_ENV)
        env_path = path.parent / ".env"
        channel_id = str(settings.get("channel_id") or "").strip()
        guild_id = str(settings.get("guild_id") or "").strip()

        missing = [name for name, value in (("channel_id", channel_id), ("guild_id", guild_id))
                   if not value]
        if missing:
            return StepResult.needs_user(
                STEP_DISCORD,
                f"Discord was selected but {' and '.join(missing)} is not configured",
                (
                    "Add `settings.discord.channel_id` and `settings.discord.guild_id` to the "
                    "install input file (Discord shows both under Copy ID with Developer Mode "
                    "on), then rerun `aq install --with discord`. Or drop `--with discord`: "
                    "AQ is fully usable without it."
                ),
                detail={"missing": missing, "credential_source": token_env},
            )
        from src.config import is_discord_snowflake

        malformed = sorted(
            name
            for name, value in (("channel_id", channel_id), ("guild_id", guild_id))
            if not is_discord_snowflake(value)
        )
        if malformed:
            # The daemon refuses a channel *name* where an id belongs, and it
            # would refuse it at the next start rather than here.  Catch it
            # while the human is still looking at the installer.
            return StepResult.needs_user(
                STEP_DISCORD,
                f"{' and '.join(malformed)} is not a Discord ID (17-20 digits)",
                (
                    "Use the numeric ID, not the channel or server name: turn on Developer "
                    "Mode in Discord, right-click the channel or server and choose Copy ID. "
                    "Then rerun `aq install --with discord`."
                ),
                detail={"malformed": malformed},
            )
        if not _token_present(token_env):
            return StepResult.needs_user(
                STEP_DISCORD,
                f"no bot token is available in {token_env}",
                (
                    f"Put the bot token in {env_path} as `{token_env}=…` (create the file with "
                    "mode 0600), then rerun `aq install --with discord`. AQ never asks for, "
                    "stores or prints the token itself — the configuration only refers to "
                    f"${{{token_env}}}."
                ),
                detail={"credential_source": token_env, "env_path": str(env_path)},
            )

        if path.exists():
            backup = backup_path_for(path)
            backup.write_bytes(path.read_bytes())
        section = dict(_read_config(path).get("discord") or {})
        section.update(
            {
                "bot_token": f"${{{token_env}}}",
                "guild_id": guild_id,
                "channel_id": channel_id,
            }
        )
        write_section(str(path), "discord", section)
        write_section(str(path), "messaging_platform", "discord")
        return StepResult.succeeded(
            STEP_DISCORD,
            f"digests and escalations will be delivered to channel {channel_id}",
            detail={
                "channel_id": channel_id,
                "guild_id": guild_id,
                "credential_source": token_env,
                "config_path": str(path),
            },
        )

    return StepSpec(
        id=STEP_DISCORD,
        title="Deliver digests and escalations to Discord",
        description=(
            "Optional. Points AQ's hourly digest and escalation threads at one channel. The bot "
            "token stays in the .env file; config.yaml only refers to it."
        ),
        run=run,
        depends_on=depends_on,
        capability=CAPABILITY_DISCORD,
        mutating=True,
        consent_prompt="Configure Discord delivery for digests and escalations?",
        verify=lambda context: _configured(),
        owner="onboarding",
    )


# ---------------------------------------------------------------------------
# daemon.start — a daemon that answers, not a process that was launched
# ---------------------------------------------------------------------------


def daemon_step(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    runner: CommandRunner | None = None,
    which: Callable[[str], str | None] | None = None,
    probe: HttpProbe | None = None,
    depends_on: tuple[str, ...] = (STEP_CHECK,),
) -> StepSpec:
    """Start the local daemon and confirm it answers ``/health``.

    "Started" is not readiness: the contract is explicit that a PID is not an
    answer.  The observable condition is the health endpoint, which is also
    what makes the step idempotent — a daemon that is already up is reused.
    """
    import shutil

    path = config_path_for(environ, home)
    lookup = which or shutil.which
    execute = runner or run_command
    check = probe or http_status

    def _base() -> str:
        return api_base_url(_read_config(path), environ)

    def _healthy() -> bool:
        return check(f"{_base()}/health") in (200, 503)

    def run(context: StepContext) -> StepResult:
        base = _base()
        if _healthy():
            return StepResult.succeeded(
                STEP_DAEMON,
                f"the daemon is already answering at {base}",
                detail={"api_url": base, "started": False},
                resources=(ResourceRecord(kind="daemon", id=base, owned=False, reused=True),),
            )
        executable = lookup("aq")
        if not executable:
            return StepResult.failed(
                STEP_DAEMON,
                "the `aq` command is not on PATH",
                (
                    "Install the AQ runtime (or activate the virtual environment that has it) "
                    "so `aq` resolves, then rerun `aq install`."
                ),
                detail={"api_url": base},
            )
        output: CommandOutput = execute([executable, "start"], timeout=DAEMON_START_TIMEOUT)
        if not output.ok or not _healthy():
            return StepResult.failed(
                STEP_DAEMON,
                redact(f"the daemon did not come up: {output.message()}"),
                (
                    f"Read {path.parent / 'daemon.log'} (or `aq logs`) for the reason, fix "
                    "it, and rerun `aq install`. `aq doctor` explains the common ones — a "
                    "database that is not reachable is the usual answer."
                ),
                detail={"api_url": base, "started": False},
            )
        return StepResult.succeeded(
            STEP_DAEMON,
            f"the daemon is answering at {base}",
            detail={"api_url": base, "started": True},
            resources=(ResourceRecord(kind="daemon", id=base, owned=True),),
        )

    return StepSpec(
        id=STEP_DAEMON,
        title="Start the AQ daemon",
        description=(
            "Optional. Runs `aq start` and waits for the daemon's /health endpoint. A daemon "
            "that is already answering is reused, never restarted."
        ),
        run=run,
        depends_on=depends_on,
        capability=CAPABILITY_DAEMON,
        mutating=True,
        consent_prompt="Start the AQ daemon now?",
        verify=lambda context: _healthy(),
        owner="onboarding",
    )


# ---------------------------------------------------------------------------
# daemon.dashboard — the URL to open, and how to get one if there is none
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DashboardInfo:
    """Where the dashboard is, or what to run to get one."""

    url: str
    reachable: bool
    source: str  # "bundled" | "dev-server" | "unknown"
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "reachable": self.reachable,
            "source": self.source,
            "hint": self.hint,
        }


def inspect_dashboard(base: str, probe: HttpProbe) -> DashboardInfo:
    """Classify what the daemon at *base* serves at ``/dashboard``.

    A release install serves a verified bundle there.  A source checkout
    deliberately ships no built assets, so the same probe answers 404 and the
    honest report is the Vite command, not a URL that would 404 in a browser.
    """
    url = f"{base}{DASHBOARD_PATH}"
    status = probe(url)
    if status is None:
        return DashboardInfo(
            url=url,
            reachable=False,
            source="unknown",
            hint=(
                "The daemon is not answering yet. Run `aq start`, then open the URL above; "
                "`aq status` reports what the daemon thinks of itself."
            ),
        )
    if status == 404:
        return DashboardInfo(
            url=SOURCE_DASHBOARD_URL,
            reachable=False,
            source="dev-server",
            hint=(
                "This is a source checkout, which ships no built dashboard. Run "
                "`npm -w dashboard run dev` in the checkout and open the URL above."
            ),
        )
    return DashboardInfo(url=url, reachable=status < 400, source="bundled")


def dashboard_step(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    probe: HttpProbe | None = None,
    depends_on: tuple[str, ...] = (STEP_CHECK,),
) -> StepSpec:
    """Report the dashboard URL.  Informational: it never blocks an install."""
    path = config_path_for(environ, home)
    check = probe or http_status

    def run(context: StepContext) -> StepResult:
        base = api_base_url(_read_config(path), environ)
        info = inspect_dashboard(base, check)
        summary = (
            f"open {info.url}"
            if info.reachable
            else f"dashboard at {info.url} — {info.hint.splitlines()[0]}"
        )
        return StepResult.succeeded(
            STEP_DASHBOARD,
            summary,
            detail={"api_url": base, "dashboard": info.to_dict()},
        )

    return StepSpec(
        id=STEP_DASHBOARD,
        title="Report the dashboard URL",
        description=(
            "Names the URL to open in a browser: the daemon serves the packaged dashboard at "
            "/dashboard, and a source checkout uses the Vite dev server instead."
        ),
        run=run,
        depends_on=depends_on,
        # Read-only, like config.check: a rerun re-probes and reports the URL
        # it observed rather than carrying a previous run's word forward with
        # no detail, which is what left the summary's Dashboard block — and
        # the first-task dashboard check — empty on a second install.
        verify=run,
        owner="onboarding",
    )


def onboarding_steps(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    runner: CommandRunner | None = None,
    which: Callable[[str], str | None] | None = None,
    probe: HttpProbe | None = None,
    depends_on: tuple[str, ...] = (STEP_DATA_DIR,),
) -> tuple[StepSpec, ...]:
    """The onboarding steps, in the order they run."""
    return (
        config_step(environ=environ, home=home, depends_on=depends_on),
        check_step(environ=environ, home=home),
        discord_step(environ=environ, home=home),
        daemon_step(environ=environ, home=home, runner=runner, which=which, probe=probe),
        dashboard_step(environ=environ, home=home, probe=probe),
    )


__all__ = [
    "CAPABILITY_DAEMON",
    "CAPABILITY_DISCORD",
    "DASHBOARD_PATH",
    "DEFAULT_API_HOST",
    "DEFAULT_API_PORT",
    "DEFAULT_WORKSPACE_DIR",
    "SOURCE_DASHBOARD_URL",
    "STEP_CHECK",
    "STEP_CONFIG",
    "STEP_DAEMON",
    "STEP_DASHBOARD",
    "STEP_DISCORD",
    "DashboardInfo",
    "HttpProbe",
    "Location",
    "api_base_url",
    "check_step",
    "config_path_for",
    "config_step",
    "daemon_step",
    "dashboard_step",
    "data_locations",
    "discord_step",
    "http_status",
    "inspect_dashboard",
    "onboarding_steps",
]
