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

import json
import os
import shutil
import sys
import time
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
STEP_PROJECT_ROOT = "config.project-root"
STEP_CHECK = "config.check"
STEP_DISCORD = "config.discord"
STEP_DAEMON = "daemon.start"
STEP_DASHBOARD = "daemon.dashboard"

#: What ``daemon.dashboard`` found (``onboarding.dashboard.source`` in the
#: result).  The daemon is API-only, so the dashboard comes from the dashboard
#: server or from nowhere yet (docs/specs/dashboard-server.md §5, §6.1).
DASHBOARD_SERVER = "dashboard-server"
#: No bundle to serve: a source checkout `dashboard.build` has not built yet.
DASHBOARD_UNBUILT = "unbuilt"
#: A bundle is installed but no dashboard server answers for it.
DASHBOARD_STOPPED = "stopped"
#: Something that is not the dashboard server answers on its port.
DASHBOARD_PORT_CONFLICT = "port-conflict"
#: ``dashboard.server.enabled: false``: the operator serves it themselves.
DASHBOARD_DISABLED = "disabled"
#: ``dashboard.server`` does not load; the hint names why.
DASHBOARD_MISCONFIGURED = "misconfigured"

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


def describe_project_root(root: Any) -> dict[str, Any]:
    """One configured project root as non-secret, JSON-safe facts.

    ``readable`` and ``writable`` are properties on :class:`~src.config.ProjectRoot`
    that query the filesystem when read, so they are resolved here — at the
    moment the installer observed them — rather than left as live callables in
    a result payload.
    """
    return {
        "id": str(getattr(root, "id", "")),
        "label": str(getattr(root, "label", "")),
        "path": str(getattr(root, "path", "")),
        "readable": bool(getattr(root, "readable", False)),
        "writable": bool(getattr(root, "writable", False)),
    }


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

#: ``identify(url)`` -> who answers ``/__aq/health`` at a dashboard server URL,
#: as :func:`src.dashboard_server.process.probe_identity` reports it:
#: ``("ours", identity)``, ``("foreign", None)`` or ``("none", None)``.
IdentityProbe = Callable[[str], "tuple[str, dict[str, Any] | None]"]

#: ``uptime(url)`` -> seconds the daemon answering ``url`` (its /health) has
#: been running, or ``None`` when that cannot be read.
UptimeReader = Callable[[str], float | None]


def daemon_uptime(url: str, timeout: float = 2.0) -> float | None:
    """``uptime_seconds`` from a daemon's /health body, or ``None``.

    A degraded daemon answers 503 with the same body, so that is read too.
    Only this one number is taken from the response.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            payload = json.load(error)
        except ValueError:
            return None
        finally:
            error.close()
    except (OSError, ValueError):
        return None
    value = payload.get("uptime_seconds") if isinstance(payload, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


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


def dashboard_server_identity(url: str) -> tuple[str, dict[str, Any] | None]:
    """Who answers ``/__aq/health`` at the dashboard server URL *url*.

    The same probe ``aq status`` and ``aq dashboard start`` use, so the
    installer never calls a process "the dashboard server" that they would not.
    """
    from src.dashboard_server.process import probe_identity

    return probe_identity(url)


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

#: The session provider ``aq install`` selects once it has seen tmux.  It is
#: deliberately *not* :class:`~src.config.SessionsConfig`'s code default:
#: that has to be a provider ``default_session_registry`` can build on any
#: host — including Windows, where the tmux module does not import — so it is
#: ``subprocess``, where attach, peek, nudge and re-adoption after a daemon
#: restart are all unavailable.  Closing that gap belongs to the installer,
#: which has already required tmux (``prereq.tmux``) and can see it on PATH.
SESSION_PROVIDER = "tmux"


def _session_provider_plan(path: Path, which: Callable[[str], str | None]) -> dict[str, Any] | None:
    """The ``sessions`` body that turns a fresh install into tmux sessions.

    Returns ``None`` when there is nothing to write: no tmux on this host, a
    host whose registry cannot build the provider, or a ``sessions.provider``
    the operator has already chosen — this step fills gaps, it never
    overwrites an opinion.  Any other keys already under ``sessions`` are
    carried through, because :func:`~src.config_editor.write_section`
    replaces the whole section.

    The value cannot come from :mod:`src.config_tuning`: a recommendation
    there must be a function of cores and RAM alone so it stays portable
    across boxes, and ``sessions`` is not a portable section. Whether *this*
    host has tmux is exactly the kind of local fact that must not travel in
    an ``.aqbundle``.
    """
    if which("tmux") is None:
        return None
    try:
        from src.sessions import default_session_registry

        if default_session_registry().get(SESSION_PROVIDER) is None:
            return None
    except Exception:  # noqa: BLE001 - an unbuildable registry is not a reason to fail
        return None
    current = _read_config(path).get("sessions")
    body = dict(current) if isinstance(current, dict) else {}
    if "provider" in body:
        return None
    body["provider"] = SESSION_PROVIDER
    return body


def _introduces_no_error(path: Path, section: str, body: Any) -> bool:
    """True when writing ``section`` adds no *new* load error to ``path``.

    Same rule as :func:`src.config_tuning.apply_tuning`: the file this runs
    against is one the wizard is still assembling, so a pre-existing error
    elsewhere must not veto an unrelated write.
    """
    from src.portable_config import validate_candidate_config

    current = _read_config(path)
    candidate = dict(current)
    candidate[section] = body
    errors = validate_candidate_config(str(path), candidate)
    if not errors:
        return True
    return not set(errors) - set(validate_candidate_config(str(path), current))


def config_step(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    which: Callable[[str], str | None] | None = None,
    depends_on: tuple[str, ...] = (STEP_DATA_DIR,),
) -> StepSpec:
    """Create ``config.yaml`` when it is absent and give it resource-aware defaults.

    The defaults come from :mod:`src.config_tuning`, which derives them from
    the box's cores and RAM — the same values ``aq system config tune --apply``
    writes.  A section the operator has already written is *kept*: this step
    fills gaps, it does not overwrite an opinion.

    One key does not come from that module: ``sessions.provider``.  ``aq
    install`` requires tmux, so once it is on PATH this step selects it
    (:func:`_session_provider_plan`) and the install ends with attachable,
    restart-surviving sessions rather than the portable ``subprocess``
    default the daemon has to fall back to on an unconfigured host.
    """
    path = config_path_for(environ, home)
    lookup = which or shutil.which

    def _plan_is_complete() -> bool:
        from src.config_tuning import MachineResources, tuning_plan

        if not path.exists():
            return False
        try:
            current = _read_config(path)
            plans = tuning_plan(current, MachineResources.detect())
            # A rerun on a host that has since grown a tmux binary still owes
            # the provider selection, so it is part of "satisfied" too.
            if _session_provider_plan(path, lookup) is not None:
                return False
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
        sessions_body = _session_provider_plan(path, lookup)
        # The contract asks for a versioned backup *before* AQ's configuration
        # is changed — and only then.  A run that has nothing to add must not
        # leave a numbered copy behind on every rerun.
        if not created and (
            sessions_body is not None
            or any(
                plan.action in ("add", "replace")
                for plan in tuning_plan(_read_config(path), machine)
            )
        ):
            backup_path_for(path).write_bytes(path.read_bytes())

        try:
            outcome = apply_tuning(str(path), machine)
        except Exception as error:  # noqa: BLE001 - untuned defaults still run
            return StepResult.succeeded(
                STEP_CONFIG,
                f"{path.name} is in place; default tuning could not be written ({error})",
                detail={
                    "config_path": str(path),
                    "created": created,
                    "tuned": False,
                    "session_provider": None,
                },
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
        # tmux is a prerequisite of this installer, so a stock install should
        # not finish on the provider that cannot attach, peek or nudge.  The
        # write is skipped rather than fatal when it would not load: an
        # install that reaches a running daemon on `subprocess` is still a
        # working install.
        session_provider = None
        if sessions_body is not None and _introduces_no_error(path, "sessions", sessions_body):
            from src.config_editor import write_section

            write_section(str(path), "sessions", sessions_body)
            written.append("sessions")
            written.sort()
            session_provider = SESSION_PROVIDER
        summary = (
            f"{'created' if created else 'using'} {path} — "
            f"{machine.cores} cores, {machine.memory_gb:.0f} GiB ({machine.size_class}): "
            f"{machine.concurrent_agents} concurrent agent(s), {machine.test_slots} test slot(s)"
        )
        if session_provider:
            summary += f"; sessions run under {session_provider}"
        return StepResult.succeeded(
            STEP_CONFIG,
            summary,
            detail={
                "config_path": str(path),
                "created": created,
                "tuned": True,
                "written": written,
                "kept": list(outcome.get("kept") or []),
                "session_provider": session_provider,
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
            "this box's cores and memory, and selects the tmux session provider so agent "
            "sessions can be attached, peeked and nudged. Sections you have already written "
            "are kept."
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


# ---------------------------------------------------------------------------
# config.project-root — where the operator's code projects live
# ---------------------------------------------------------------------------


def project_root_step(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    depends_on: tuple[str, ...] = (STEP_CONFIG,),
) -> StepSpec:
    """Record the projects folder the wizard asked for as a project root.

    Every fresh install used to finish with "No project root is configured",
    leaving the newcomer to hand-edit ``project_roots`` before the first project
    could be created.  The wizard now asks for the folder (install setting
    ``project_root``), and this step writes it -- creating the folder if it does
    not exist yet -- next to any roots already configured.  With no folder
    chosen it changes nothing, and a configured root is never replaced.
    """
    path = config_path_for(environ, home)

    def _roots() -> list[dict[str, Any]]:
        roots = _read_config(path).get("project_roots") or []
        return [dict(root) for root in roots if isinstance(root, Mapping)]

    def _chosen(context: StepContext) -> Path | None:
        raw = context.option("project_root")
        return Path(str(raw)).expanduser() if raw else None

    def _includes(roots: list[dict[str, Any]], folder: Path) -> bool:
        return any(Path(str(root.get("path") or "")).expanduser() == folder for root in roots)

    def run(context: StepContext) -> StepResult:
        from src.config_editor import write_section

        from .wizard import project_root_entry

        roots = _roots()
        folder = _chosen(context)
        if folder is None:
            summary = (
                f"projects folder already configured: {roots[0].get('path')}"
                if roots
                else "no projects folder chosen; add one under Settings → Project Roots"
            )
            return StepResult.succeeded(
                STEP_PROJECT_ROOT, summary, detail={"configured": bool(roots)}
            )
        if _includes(roots, folder):
            return StepResult.succeeded(
                STEP_PROJECT_ROOT,
                f"{folder} is already a project root",
                detail={"configured": True, "path": str(folder)},
            )
        if context.dry_run:
            return StepResult.succeeded(
                STEP_PROJECT_ROOT,
                f"would add {folder} as a project root",
                detail={"configured": False, "path": str(folder), "dry_run": True},
            )
        if not path.exists():
            return StepResult.failed(
                STEP_PROJECT_ROOT,
                f"{path} does not exist, so the projects folder cannot be recorded",
                f"Rerun `aq install` so `{STEP_CONFIG}` writes the configuration first.",
            )
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            return StepResult.failed(
                STEP_PROJECT_ROOT,
                f"could not create the projects folder {folder}: {error}",
                f"Create {folder} (or choose another folder) and rerun `aq install`.",
            )
        entry = project_root_entry(folder, (str(root.get("id")) for root in roots))
        write_section(str(path), "project_roots", [*roots, entry])
        return StepResult.succeeded(
            STEP_PROJECT_ROOT,
            f"projects folder {folder} is project root `{entry['id']}`",
            detail={"configured": True, "path": str(folder), "root_id": entry["id"]},
        )

    def verify(context: StepContext) -> bool:
        roots = _roots()
        folder = _chosen(context)
        return _includes(roots, folder) if folder is not None else bool(roots)

    return StepSpec(
        id=STEP_PROJECT_ROOT,
        title="Record your projects folder",
        description=(
            "Adds the folder your code projects live in as a project root, so the first "
            "project can be created from the dashboard. Existing roots are kept."
        ),
        run=run,
        depends_on=depends_on,
        # Not consent-gated: the folder is the answer the person just gave, and
        # with no answer the step changes nothing.  A dry run writes nothing.
        verify=verify,
        owner="onboarding",
    )


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
        # Readiness condition 6 asks whether "configured project roots are
        # readable and writable where project creation needs them".  This step
        # is the one place that already holds a loaded ``AppConfig``, so it
        # records the non-secret facts and the wizard judges them; an install
        # that configured none records an empty list, which is the honest
        # answer rather than an absent key.
        detail["project_roots"] = [describe_project_root(root) for root in config.project_roots]
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

        missing = [
            name
            for name, value in (("channel_id", channel_id), ("guild_id", guild_id))
            if not value
        ]
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


def aq_aware_which(
    base: Callable[[str], str | None],
    *,
    interpreter: str | None = None,
) -> Callable[[str], str | None]:
    """``which`` that can always find the ``aq`` this installer is running as.

    ``aq install`` starts the daemon by running ``aq start``, and looking that up
    on PATH alone failed with "the `aq` command is not on PATH" whenever the
    installer itself had been invoked by path -- ``~/.local/bin/aq`` from a shell
    whose profile has not been re-read, or a virtualenv that was never activated.
    The console script beside the running interpreter *is* the same
    installation, so it is the honest answer.
    """
    python = Path(interpreter or sys.executable)

    def which(command: str) -> str | None:
        found = base(command)
        if found:
            return found
        if command not in ("aq", "agent-queue"):
            return None
        candidate = python.with_name(command)
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
        return None

    return which


def daemon_step(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    runner: CommandRunner | None = None,
    which: Callable[[str], str | None] | None = None,
    probe: HttpProbe | None = None,
    uptime: UptimeReader | None = None,
    watched: Callable[[], list[Path]] | None = None,
    clock: Callable[[], float] = time.time,
    depends_on: tuple[str, ...] = (STEP_CHECK,),
) -> StepSpec:
    """Start the local daemon and confirm it answers ``/health``.

    "Started" is not readiness: the contract is explicit that a PID is not an
    answer.  The observable condition is the health endpoint, which is also
    what makes the step idempotent — a daemon that is already up is reused.

    Reused, but not blindly: a daemon that started *before* this install last
    changed its configuration or its code is still running the old ones.  A
    rerun that switched ``sessions.provider`` to tmux left the dashboard's
    supervisor on the subprocess provider ("does not support interactive
    terminal input") because the running daemon was never restarted.  Such a
    daemon is restarted; `aq restart` re-adopts live agent sessions.
    """
    path = config_path_for(environ, home)
    lookup = aq_aware_which(which or shutil.which)
    execute = runner or run_command
    check = probe or http_status
    # A test that injects its own probe is talking to a fake daemon; reading
    # uptime over the network behind that fake's back would reach a real one.
    read_uptime = uptime or (daemon_uptime if probe is None else (lambda url: None))

    def _watched() -> list[Path]:
        if watched is not None:
            return watched()
        from .dashboard import source_checkout_root

        paths = [path]
        checkout = source_checkout_root()
        if checkout is not None:
            # `git reset` (the bootstrap's update) rewrites the index.
            paths.append(checkout / ".git" / "index")
        return paths

    def _stale_reason() -> str | None:
        up = read_uptime(f"{_base()}/health")
        if up is None:
            return None
        started = clock() - up
        changed = []
        for candidate in _watched():
            try:
                if candidate.stat().st_mtime > started + 1.0:
                    changed.append(candidate)
            except OSError:
                continue
        if not changed:
            return None
        return "configuration" if changed == [path] else "updated configuration or code"

    def _base() -> str:
        return api_base_url(_read_config(path), environ)

    def _healthy() -> bool:
        return check(f"{_base()}/health") in (200, 503)

    def run(context: StepContext) -> StepResult:
        base = _base()
        if _healthy():
            stale = _stale_reason()
            if stale is None:
                return StepResult.succeeded(
                    STEP_DAEMON,
                    f"the daemon is already answering at {base}",
                    detail={"api_url": base, "started": False},
                    resources=(ResourceRecord(kind="daemon", id=base, owned=False, reused=True),),
                )
            aq = lookup("aq")
            restarted = (
                execute([aq, "restart", "--no-dashboard"], timeout=DAEMON_START_TIMEOUT)
                if aq
                else None
            )
            if restarted is None or not restarted.ok or not _healthy():
                reason = "`aq` is not on PATH" if restarted is None else restarted.message()
                return StepResult.failed(
                    STEP_DAEMON,
                    redact(f"the daemon could not be restarted to load its {stale}: {reason}"),
                    (
                        f"Run `aq restart`, then rerun `aq install`. {path.parent / 'daemon.log'} "
                        "(or `aq logs`) says why the daemon did not come back if it does not."
                    ),
                    detail={"api_url": base, "started": False},
                )
            return StepResult.succeeded(
                STEP_DAEMON,
                f"restarted the daemon at {base} to load its {stale}",
                detail={"api_url": base, "started": True, "restarted": True},
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
        # `--no-dashboard` is not a preference here, it is what makes the step
        # runnable at all: plain `aq start` asks `click.confirm` whether to
        # launch the Vite dev server when it is run from a source checkout, and
        # a confirmation with no terminal behind it raises click's Abort. An
        # unattended `aq install` therefore failed with "the daemon did not
        # come up: Aborted!" *after* the daemon had actually started (observed
        # natively on macOS 14 and 15, aq/noble-apex.18). The dashboard belongs
        # to `daemon.dashboard`, which reports it without asking anybody.
        output: CommandOutput = execute(
            [executable, "start", "--no-dashboard"], timeout=DAEMON_START_TIMEOUT
        )
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
            "that is already answering is reused, and restarted only when it started before "
            "the configuration or code last changed."
        ),
        run=run,
        depends_on=depends_on,
        capability=CAPABILITY_DAEMON,
        mutating=True,
        consent_prompt="Start the AQ daemon now?",
        verify=lambda context: _healthy() and _stale_reason() is None,
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
    #: One of the ``DASHBOARD_*`` values above.
    source: str
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "reachable": self.reachable,
            "source": self.source,
            "hint": self.hint,
        }


def inspect_dashboard(
    config: Path,
    *,
    probe: HttpProbe,
    identify: IdentityProbe,
    bundle_present: Callable[[], bool],
) -> DashboardInfo:
    """Classify what serves the dashboard this install's configuration points at.

    The daemon is API-only: it answers ``/dashboard`` with a JSON pointer and
    serves no page (docs/specs/dashboard-server.md §5).  The dashboard is
    the dashboard server's, at ``dashboard.server``'s URL, read the way the
    server itself reads it -- so this names the URL a browser will actually
    load, and when nothing serves it, the one command that fixes that.
    """
    from src.dashboard_server.process import FOREIGN, OURS, configured

    server = configured(config)
    if server.settings is None:
        return DashboardInfo(
            url="",
            reachable=False,
            source=DASHBOARD_MISCONFIGURED,
            hint=(
                f"dashboard.server in {config} does not load: {server.error}. Fix it, then run "
                "`aq dashboard start`."
            ),
        )
    url = server.settings.url
    kind, identity = identify(url)
    if kind == OURS and identity is not None:
        status = probe(url)
        if status == 200:
            daemon_down = identity.get("upstream_ok") is False
            return DashboardInfo(
                url=url,
                reachable=True,
                source=DASHBOARD_SERVER,
                hint=(
                    "The dashboard server is up, but the daemon behind it is not answering: "
                    "run `aq start`."
                    if daemon_down
                    else ""
                ),
            )
        return DashboardInfo(
            url=url,
            reachable=False,
            source=DASHBOARD_SERVER,
            hint=(
                f"The dashboard server answered {status or 'nothing'} for the URL above. Run "
                "`aq dashboard restart`; `aq dashboard status` names its log."
            ),
        )
    if not server.enabled:
        return DashboardInfo(
            url=url,
            reachable=False,
            source=DASHBOARD_DISABLED,
            hint=(
                "dashboard.server.enabled is false, so AQ does not run the dashboard server. "
                "Set it to true and run `aq dashboard start`, or serve it yourself with "
                "`aq dashboard serve`."
            ),
        )
    if not bundle_present():
        # A source checkout's bundle is built by `dashboard.build`, and
        # `dashboard.serve` then starts the dashboard server on it.  Reaching
        # this branch means those steps have not completed, and rerunning the
        # install is the fix -- not a Vite server the newcomer would have to
        # keep running by hand.
        return DashboardInfo(
            url=url,
            reachable=False,
            source=DASHBOARD_UNBUILT,
            hint=(
                "The dashboard has not been built yet. Rerun the install command: it builds "
                "the dashboard and starts the dashboard server at the URL above."
            ),
        )
    if kind == FOREIGN:
        return DashboardInfo(
            url=url,
            reachable=False,
            source=DASHBOARD_PORT_CONFLICT,
            hint=(
                f"Another program answers on port {server.settings.port}. Free it, or set "
                f"dashboard.server.port in {config}; then run `aq dashboard start`."
            ),
        )
    return DashboardInfo(
        url=url,
        reachable=False,
        source=DASHBOARD_STOPPED,
        hint=(
            "The dashboard server is not running. Run `aq dashboard start` (`aq start` starts "
            "it with the daemon); `aq dashboard status` says why if it does not stay up."
        ),
    )


def dashboard_step(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    probe: HttpProbe | None = None,
    identify: IdentityProbe | None = None,
    root: Path | None = None,
    depends_on: tuple[str, ...] = (STEP_CHECK,),
) -> StepSpec:
    """Report the dashboard URL.  Informational: it never blocks an install."""
    # Imported here, not at module level: the dashboard module builds on this
    # one's helpers, so a top-level import would be circular.
    from .dashboard import bundle_present

    path = config_path_for(environ, home)
    check = probe or http_status
    # A test that injects its own HTTP probe is talking to a fake host; asking
    # the real network who answers behind that fake's back would reach this
    # machine's own dashboard server.
    who = identify or (dashboard_server_identity if probe is None else _nobody)

    def run(context: StepContext) -> StepResult:
        base = api_base_url(_read_config(path), environ)
        info = inspect_dashboard(
            path, probe=check, identify=who, bundle_present=lambda: bundle_present(root)
        )
        first = info.hint.splitlines()[0] if info.hint else ""
        if info.reachable:
            summary = f"open {info.url}"
        elif info.url:
            summary = f"dashboard at {info.url} — {first}"
        else:
            summary = first
        return StepResult.succeeded(
            STEP_DASHBOARD,
            summary,
            detail={"api_url": base, "dashboard": info.to_dict()},
        )

    return StepSpec(
        id=STEP_DASHBOARD,
        title="Report the dashboard URL",
        description=(
            "Names the URL to open in a browser: the dashboard server's (dashboard.server, "
            "http://127.0.0.1:8082/ by default). The daemon serves only its API."
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


def _nobody(url: str) -> tuple[str, dict[str, Any] | None]:
    """An identity probe that finds nothing: the default beside an injected HTTP probe."""
    del url
    return "none", None


def onboarding_steps(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    runner: CommandRunner | None = None,
    which: Callable[[str], str | None] | None = None,
    probe: HttpProbe | None = None,
    identify: IdentityProbe | None = None,
    depends_on: tuple[str, ...] = (STEP_DATA_DIR,),
    dashboard_root: Path | None = None,
) -> tuple[StepSpec, ...]:
    """The onboarding steps, in the order they run.

    ``dashboard_root`` is the installation the dashboard is built and served
    from; ``None`` finds the one this installer runs from.  A directory that
    is not a checkout means a release install, whose dashboard ships already
    built under ``src/dashboard_assets/dist`` -- which is how a test composes
    the installer without building a real dashboard.  ``identify`` is how the
    dashboard steps ask who answers at the dashboard server's URL.
    """
    # Imported here, not at module level: the dashboard steps build on this
    # module's helpers, so a top-level import would be circular.
    from .dashboard import (
        STEP_DASHBOARD_SERVE,
        dashboard_build_step,
        dashboard_open_step,
        dashboard_serve_step,
    )

    return (
        config_step(environ=environ, home=home, which=which, depends_on=depends_on),
        project_root_step(environ=environ, home=home),
        # Checked after the projects folder is recorded, so the check loads the
        # configuration the daemon will actually start with.
        check_step(environ=environ, home=home, depends_on=(STEP_PROJECT_ROOT,)),
        discord_step(environ=environ, home=home),
        daemon_step(environ=environ, home=home, runner=runner, which=which, probe=probe),
        dashboard_build_step(
            environ=environ,
            home=home,
            runner=runner,
            which=which,
            root=dashboard_root,
        ),
        dashboard_serve_step(
            environ=environ,
            home=home,
            runner=runner,
            which=which,
            probe=probe,
            identify=identify,
            root=dashboard_root,
        ),
        # Reported after the dashboard server started, so the URL it names is
        # the one being served.
        dashboard_step(
            environ=environ,
            home=home,
            probe=probe,
            identify=identify,
            root=dashboard_root,
            depends_on=(STEP_CHECK, STEP_DASHBOARD_SERVE),
        ),
        dashboard_open_step(
            environ=environ,
            home=home,
            runner=runner,
            which=which,
            probe=probe,
            identify=identify,
        ),
    )


__all__ = [
    "CAPABILITY_DAEMON",
    "CAPABILITY_DISCORD",
    "DASHBOARD_DISABLED",
    "DASHBOARD_MISCONFIGURED",
    "DASHBOARD_PORT_CONFLICT",
    "DASHBOARD_SERVER",
    "DASHBOARD_STOPPED",
    "DASHBOARD_UNBUILT",
    "DEFAULT_API_HOST",
    "DEFAULT_API_PORT",
    "DEFAULT_WORKSPACE_DIR",
    "SESSION_PROVIDER",
    "SOURCE_DASHBOARD_URL",
    "STEP_CHECK",
    "STEP_CONFIG",
    "STEP_DAEMON",
    "STEP_DASHBOARD",
    "STEP_DISCORD",
    "STEP_PROJECT_ROOT",
    "DashboardInfo",
    "HttpProbe",
    "IdentityProbe",
    "Location",
    "UptimeReader",
    "api_base_url",
    "aq_aware_which",
    "check_step",
    "config_path_for",
    "config_step",
    "daemon_step",
    "daemon_uptime",
    "dashboard_server_identity",
    "dashboard_step",
    "data_locations",
    "describe_project_root",
    "discord_step",
    "http_status",
    "inspect_dashboard",
    "onboarding_steps",
    "project_root_step",
]
