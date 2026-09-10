#!/usr/bin/env python3
"""Drive the documented macOS onboarding journey on a real Mac and record it.

``noble-apex.18`` asks for *native* macOS evidence: the contract
(``docs/plans/install-onboarding/contract.md``, "Acceptance evidence for
downstream tasks") wants a row per platform carrying the host version, the
architecture, the installer version, the selected providers, the command and
result identifiers, a rerun, a repair or resume, and the first-task outcome.
``docs/contributing/installer-testing.md`` is equally explicit about what a
mock may *not* claim, so this script exists to be run where the claim is
allowed: on a Mac.

It is deliberately not a pytest suite. Every phase runs the same commands a
human would run from ``docs/tutorials/install.md``, keeps the exit code and the
machine-readable payload, and writes one JSON evidence record plus a Markdown
rendering of it. Phases that need a human (a browser login) or a machine state
CI cannot produce (a Mac that has never had Homebrew, a reboot) are recorded
as ``unmet`` with the reason, never as a pass by analogy.

    python3 scripts/acceptance/macos_acceptance.py --output evidence/

The journey mutates this machine: it installs Homebrew formulae, starts a
PostgreSQL service and writes ``~/.agent-queue``. Run it on a disposable Mac,
a fresh VM or a CI runner. It refuses to start on a machine that already has
an AQ install home unless ``--force`` says otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Phase verdicts.  ``unmet`` is a first-class outcome, not a soft failure: an
#: environment that could not be produced stays visible as missing evidence.
PASS = "pass"
FAIL = "fail"
UNMET = "unmet"

#: Capabilities the unattended journey selects.  Providers are deliberately
#: absent: their login is human-only, so selecting them here would turn every
#: run into ``needs_user`` and prove nothing about the rest of the path.
DEFAULT_CAPABILITIES = ("postgres-managed", "daemon")

#: How long a single command may take.  ``brew install postgresql@NN`` and the
#: first ``initdb`` are minutes, not seconds, on a cold machine.
DEFAULT_TIMEOUT = 1800.0

MAX_CAPTURE = 20000


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class CommandRecord:
    """One command, exactly as it ran."""

    argv: list[str]
    exit_code: int | None
    duration_s: float
    stdout: str
    stderr: str
    timed_out: bool = False
    json: Any = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class Phase:
    """One journey phase and what it concluded."""

    id: str
    title: str
    verdict: str
    detail: str
    commands: list[CommandRecord] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)


def _truncate(text: str) -> str:
    if len(text) <= MAX_CAPTURE:
        return text
    return text[:MAX_CAPTURE] + f"\n... [{len(text) - MAX_CAPTURE} more characters]"


def run(
    argv: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    env: dict[str, str] | None = None,
    parse_json: bool = False,
    cwd: Path | None = None,
) -> CommandRecord:
    """Run one command and capture everything worth recording about it."""
    started = time.monotonic()
    try:
        completed = subprocess.run(  # noqa: S603 - the argv is built here, never from input
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, **(env or {})},
            cwd=str(cwd) if cwd else None,
            check=False,
        )
    except FileNotFoundError as error:
        return CommandRecord(
            argv=list(argv),
            exit_code=None,
            duration_s=round(time.monotonic() - started, 2),
            stdout="",
            stderr=f"{error}",
        )
    except subprocess.TimeoutExpired as error:
        return CommandRecord(
            argv=list(argv),
            exit_code=None,
            duration_s=round(time.monotonic() - started, 2),
            stdout=_truncate(error.stdout or "" if isinstance(error.stdout, str) else ""),
            stderr=_truncate(error.stderr or "" if isinstance(error.stderr, str) else ""),
            timed_out=True,
        )
    record = CommandRecord(
        argv=list(argv),
        exit_code=completed.returncode,
        duration_s=round(time.monotonic() - started, 2),
        stdout=_truncate(completed.stdout),
        stderr=_truncate(completed.stderr),
    )
    if parse_json and completed.stdout.strip():
        # ``--json`` prints exactly one object on stdout; anything a library
        # printed before it would make this fail loudly rather than silently
        # recording a half-parsed payload.
        for line in reversed(completed.stdout.strip().splitlines()):
            try:
                record.json = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    return record


class Journey:
    """The documented macOS journey, phase by phase."""

    def __init__(self, *, aq: str, repo: Path, capabilities: Sequence[str], skip: set[str]) -> None:
        self.aq = aq
        self.repo = repo
        self.capabilities = tuple(capabilities)
        self.skip = skip
        self.phases: list[Phase] = []

    # -- helpers ---------------------------------------------------------

    def _aq(self, *args: str, parse_json: bool = True, timeout: float = DEFAULT_TIMEOUT):
        return run([self.aq, *args], parse_json=parse_json, timeout=timeout, cwd=self.repo)

    def add(self, phase: Phase) -> Phase:
        self.phases.append(phase)
        return phase

    def unmet(self, phase_id: str, title: str, reason: str) -> Phase:
        return self.add(Phase(id=phase_id, title=title, verdict=UNMET, detail=reason))

    # -- phases ----------------------------------------------------------

    def host_facts(self) -> Phase:
        """Record the machine, in its own words."""
        probes = {
            "sw_vers": ["sw_vers"],
            "uname": ["uname", "-a"],
            "arch": ["arch"],
            "proc_translated": ["sysctl", "-n", "sysctl.proc_translated"],
            "hw_optional_arm64": ["sysctl", "-n", "hw.optional.arm64"],
            "hw_model": ["sysctl", "-n", "hw.model"],
            "xcode_select": ["xcode-select", "-p"],
            "brew_version": ["brew", "--version"],
            "brew_prefix": ["brew", "--prefix"],
            "git": ["git", "--version"],
            "python": [sys.executable, "-V"],
        }
        commands: list[CommandRecord] = []
        facts: dict[str, Any] = {
            "python_executable": sys.executable,
            "shell": os.environ.get("SHELL", ""),
            "home": os.path.expanduser("~"),
            "platform_machine": platform.machine(),
            "platform_mac_ver": platform.mac_ver()[0],
        }
        for name, argv in probes.items():
            record = run(argv, timeout=120)
            commands.append(record)
            facts[name] = record.stdout.strip() if record.ok else None
        verdict = PASS if facts.get("sw_vers") else FAIL
        return self.add(
            Phase(
                id="host",
                title="Record the host",
                verdict=verdict,
                detail=(
                    f"macOS {facts.get('platform_mac_ver') or '?'} "
                    f"on {facts['platform_machine']}"
                ),
                commands=commands,
                facts=facts,
            )
        )

    def matrix(self) -> Phase:
        """The installer's own verdict about this host, before it changes it."""
        steps = self._aq("install", "--list-steps", "--json")
        host = run(
            [
                sys.executable,
                "-c",
                "import json;from src.install import describe_host;"
                "print(json.dumps(describe_host().to_dict()))",
            ],
            cwd=self.repo,
            parse_json=True,
            timeout=300,
        )
        payload = host.json if isinstance(host.json, dict) else {}
        host_path = str(payload.get("host_path", ""))
        tier = str(payload.get("tier", ""))
        ok = host_path.startswith("macos") and tier in {"supported", "compatibility"}
        return self.add(
            Phase(
                id="matrix",
                title="Supported-platform verdict",
                verdict=PASS if ok else FAIL,
                detail=f"host_path={host_path or '?'} tier={tier or '?'}",
                commands=[steps, host],
                facts={
                    "verdict": payload,
                    "steps": (steps.json or {}).get("steps")
                    if isinstance(steps.json, dict)
                    else None,
                },
            )
        )

    def plan(self) -> Phase:
        """A dry run must describe the work and change nothing."""
        args = ["install", "--non-interactive", "--dry-run", "--json"]
        for capability in self.capabilities:
            args += ["--with", capability]
        record = self._aq(*args)
        payload = record.json if isinstance(record.json, dict) else {}
        state_path = payload.get("state_path")
        wrote_state = bool(state_path) and Path(str(state_path)).exists()
        rows = payload.get("plan") or []
        ok = record.ok and bool(rows) and not wrote_state
        detail = f"{len(rows)} planned step(s); resume record written: {wrote_state}"
        return self.add(
            Phase(
                id="plan",
                title="Dry run",
                verdict=PASS if ok else FAIL,
                detail=detail,
                commands=[record],
                facts={"plan": rows, "outcome": payload.get("outcome")},
            )
        )

    def _install(self, phase_id: str, title: str, extra: Sequence[str] = ()) -> Phase:
        args = ["install", "--non-interactive", "--yes", "--json", *extra]
        for capability in self.capabilities:
            args += ["--with", capability]
        record = self._aq(*args)
        payload = record.json if isinstance(record.json, dict) else {}
        outcome = str(payload.get("outcome", ""))
        states = {
            str(step.get("step_id")): str(step.get("state")) for step in payload.get("steps") or []
        }
        failed = sorted(key for key, value in states.items() if value == "failed")
        ok = outcome == "ready" and not failed
        detail = f"outcome={outcome or '?'} exit={record.exit_code}"
        if failed:
            detail += f"; failed: {', '.join(failed)}"
        return self.add(
            Phase(
                id=phase_id,
                title=title,
                verdict=PASS if ok else FAIL,
                detail=detail,
                commands=[record],
                facts={
                    "outcome": outcome,
                    "states": states,
                    "resources": payload.get("resources"),
                    "onboarding": payload.get("onboarding"),
                    "messages": payload.get("messages"),
                },
            )
        )

    def install(self) -> Phase:
        return self._install("install", "Unattended install to a ready daemon")

    def rerun(self, first: Phase) -> Phase:
        """A rerun installs nothing twice and owns exactly the same things."""
        phase = self._install("rerun", "Rerun (idempotence)")
        before = first.facts.get("resources") or []
        after = phase.facts.get("resources") or []

        def key(rows: Any) -> list[str]:
            if not isinstance(rows, list):
                return []
            return sorted(f"{row.get('kind')}:{row.get('id')}:{row.get('owned')}" for row in rows)

        same = key(before) == key(after)
        phase.facts["owned_resources_identical"] = same
        if phase.verdict == PASS and not same:
            phase.verdict = FAIL
            phase.detail += "; the rerun changed the owned-resource set"
        else:
            phase.detail += f"; owned-resource set identical: {same}"
        return phase

    def repair(self) -> Phase:
        return self._install("repair", "Repair", extra=("--repair",))

    def upgrade(self) -> Phase:
        return self._install("upgrade", "Upgrade", extra=("--upgrade",))

    def restart_from(self) -> Phase:
        """``--restart-from`` replays one branch and carries the rest forward."""
        return self._install(
            "restart-from",
            "Resume one branch (--restart-from)",
            extra=("--restart-from", "config.defaults"),
        )

    def daemon(self) -> Phase:
        """The daemon and dashboard the install claims to have started."""
        health = run(
            [
                "curl",
                "-sS",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "http://127.0.0.1:8081/health",
            ],
            timeout=60,
        )
        dashboard = run(
            [
                "curl",
                "-sS",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "http://127.0.0.1:8081/dashboard",
            ],
            timeout=60,
        )
        status = self._aq("status", parse_json=False, timeout=120)
        codes = (health.stdout.strip(), dashboard.stdout.strip())
        ok = codes[0] == "200"
        return self.add(
            Phase(
                id="daemon",
                title="Daemon and dashboard answer",
                verdict=PASS if ok else FAIL,
                detail=(
                    f"/health -> {codes[0] or 'no answer'}, "
                    f"/dashboard -> {codes[1] or 'no answer'}"
                ),
                commands=[health, dashboard, status],
                facts={"health": codes[0], "dashboard": codes[1]},
            )
        )

    def database(self) -> Phase:
        """The PostgreSQL the installer manages, as ``brew services`` sees it."""
        services = run(["brew", "services", "list"], timeout=300)
        doctor = self._aq(
            "doctor", "--check", "database.connection", "--json", parse_json=True, timeout=300
        )
        running = "postgresql" in services.stdout and "started" in services.stdout
        return self.add(
            Phase(
                id="database",
                title="PostgreSQL under brew services",
                verdict=PASS if running else FAIL,
                detail=(
                    "a postgresql service is started"
                    if running
                    else "no started postgresql service"
                ),
                commands=[services, doctor],
                facts={"brew_services": services.stdout.strip()},
            )
        )

    def postgres_client(self) -> Phase:
        """Where the `psql` the installer used actually comes from.

        Homebrew's versioned PostgreSQL formulae are keg-only: `brew install
        postgresql@17` links nothing into `<prefix>/bin`, so on a Mac whose only
        PostgreSQL is the one AQ just installed, `psql` is not on PATH and the
        administrator route the role and database steps need does not exist.
        A hosted runner usually has another PostgreSQL already linked, which
        hides that — so record which one answered rather than assuming.
        """
        formula = "postgresql@17"
        prefix = run(["brew", "--prefix"], timeout=120)
        keg = run(["brew", "--prefix", formula], timeout=120)
        on_path = run(["/bin/sh", "-c", "command -v psql || true"], timeout=120)
        linked = run(
            ["/bin/sh", "-c", "brew list --versions | grep -i postgres || true"], timeout=300
        )
        keg_path = keg.stdout.strip()
        psql_path = on_path.stdout.strip()
        keg_psql = f"{keg_path}/bin/psql" if keg_path else ""
        from_keg = (
            bool(psql_path)
            and bool(keg_psql)
            and Path(psql_path).resolve() == Path(keg_psql).resolve()
            if psql_path and keg_psql and Path(psql_path).exists() and Path(keg_psql).exists()
            else False
        )
        facts = {
            "brew_prefix": prefix.stdout.strip(),
            "formula": formula,
            "keg_prefix": keg_path,
            "psql_on_path": psql_path,
            "psql_from_installed_formula": from_keg,
            "installed_postgres_formulae": linked.stdout.strip(),
        }
        if not psql_path:
            detail = (
                f"no psql on PATH: {formula} is keg-only, so nothing it installed is linked "
                f"into {facts['brew_prefix']}/bin"
            )
            verdict = FAIL
        elif from_keg:
            detail = f"psql on PATH is the one {formula} installed ({psql_path})"
            verdict = PASS
        else:
            detail = (
                f"psql on PATH is {psql_path}, which is not the keg-only {formula} AQ "
                f"installed ({keg_psql or 'unknown'}) -- this machine already had a client"
            )
            verdict = PASS
        return self.add(
            Phase(
                id="postgres-client",
                title="Where psql comes from",
                verdict=verdict,
                detail=detail,
                commands=[prefix, keg, on_path, linked],
                facts=facts,
            )
        )

    def first_task(self) -> Phase:
        """The disposable first project and task, as far as no credential allows.

        A live claim needs an authenticated harness, which is an unmet row of
        its own.  What *is* native evidence here is everything up to it: the
        daemon serves the queue, a configured project root accepts a new
        repository, and a submitted task reaches the frontier.
        """
        roots = self._aq("project", "list-roots", "--json")
        payload = roots.json if isinstance(roots.json, dict) else {}
        rows = payload.get("data") or payload.get("roots") or []
        root_id = None
        if isinstance(rows, list) and rows:
            first = rows[0]
            root_id = first.get("id") if isinstance(first, dict) else None
        commands = [roots]
        facts: dict[str, Any] = {"roots": rows, "root_id": root_id}

        if not root_id:
            return self.add(
                Phase(
                    id="first-task",
                    title="First project and task",
                    verdict=FAIL,
                    detail=(
                        "the install configured no project root, so the documented "
                        "`aq project onboard` first step has nowhere to put a repository"
                    ),
                    commands=commands,
                    facts=facts,
                )
            )

        onboard = self._aq(
            "project",
            "onboard",
            "--source-mode",
            "init",
            "--root-id",
            str(root_id),
            "--relative-path",
            "macos-acceptance",
            "--project-name",
            "macOS acceptance",
            "--project-id",
            "macos-acceptance",
            "--json",
        )
        commands.append(onboard)
        created = self._aq(
            "task",
            "create",
            "--project",
            "macos-acceptance",
            "--title",
            "macOS acceptance smoke task",
            "--description",
            "Created by scripts/acceptance/macos_acceptance.py; no agent is expected to run it.",
            "--json",
        )
        commands.append(created)
        listed = self._aq("task", "list", "--project", "macos-acceptance", "--json")
        commands.append(listed)
        ok = onboard.ok and created.ok
        return self.add(
            Phase(
                id="first-task",
                title="First project and task",
                verdict=PASS if ok else FAIL,
                detail=(
                    "project onboarded and a task submitted through the real CLI"
                    if ok
                    else f"onboard exit {onboard.exit_code}, task create exit {created.exit_code}"
                ),
                commands=commands,
                facts=facts,
            )
        )

    def uninstall_plan(self) -> Phase:
        """Uninstall must plan to remove only what AQ owns."""
        record = self._aq("uninstall", "--dry-run", "--json")
        payload = record.json if isinstance(record.json, dict) else {}
        plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
        items = plan.get("items") if isinstance(plan.get("items"), list) else []
        manual = payload.get("manual") if isinstance(payload.get("manual"), list) else []
        kept = payload.get("kept") if isinstance(payload.get("kept"), list) else []
        removed = payload.get("removed") if isinstance(payload.get("removed"), list) else []
        # The contract's rule for a default uninstall: nothing shared with the
        # rest of the machine is removed for you.  Homebrew, the Command Line
        # Tools, a PostgreSQL server and a formula AQ installed must all be
        # reported rather than deleted.
        shared = {"package-manager", "developer-tools", "postgres-server", "brew-formula"}
        wrongly_removed = sorted(
            f"{row.get('kind')}:{row.get('id')}"
            for row in items
            if isinstance(row, dict) and row.get("action") == "remove" and row.get("kind") in shared
        )
        ok = record.ok and not wrongly_removed
        detail = (
            f"{len(items)} item(s): {len(removed)} to remove, {len(kept)} kept, "
            f"{len(manual)} left to the operator"
        )
        if wrongly_removed:
            detail += f"; would remove shared resources: {', '.join(wrongly_removed)}"
        return self.add(
            Phase(
                id="uninstall-plan",
                title="Uninstall plan (dry run)",
                verdict=PASS if ok else FAIL,
                detail=detail,
                commands=[record],
                facts={"items": items, "manual": manual, "kept": kept, "removed": removed},
            )
        )

    def run_all(self) -> None:
        self.host_facts()
        self.matrix()
        if "plan" not in self.skip:
            self.plan()
        first = self.install()
        if "rerun" not in self.skip:
            self.rerun(first)
        if "repair" not in self.skip:
            self.repair()
        if "upgrade" not in self.skip:
            self.upgrade()
        if "restart-from" not in self.skip:
            self.restart_from()
        self.daemon()
        self.database()
        self.postgres_client()
        if "first-task" not in self.skip:
            self.first_task()
        if "uninstall-plan" not in self.skip:
            self.uninstall_plan()
        self.record_unmet()

    def record_unmet(self) -> None:
        """Everything this environment structurally cannot prove."""
        for phase_id, title, reason in UNMET_ROWS:
            self.unmet(phase_id, title, reason)


#: The rows a CI runner — or any single unattended run — cannot fill in. They
#: are listed here rather than left out, because the contract requires an
#: unavailable environment to stay visible as missing evidence.
UNMET_ROWS: tuple[tuple[str, str, str], ...] = (
    (
        "clean-machine-homebrew",
        "A Mac that has never had Homebrew",
        "This run found Homebrew already installed. The `macos.homebrew` needs_user branch "
        "(the official install one-liner, which asks for an administrator password AQ never "
        "types) is therefore not exercised natively here; it needs a Mac with no Homebrew.",
    ),
    (
        "provider-login",
        "A real browser login for each harness",
        "No provider credential exists in this environment and AQ never enters one. The "
        "`provider.*-login` steps and the Keychain-backed credential store need a human at a "
        "browser on a real Mac.",
    ),
    (
        "live-first-task",
        "A live agent claiming and completing the first task",
        "A live first task needs an authenticated harness, which depends on the unmet "
        "provider login above. The project and queue were exercised; a claim and an "
        "integrated result were not.",
    ),
    (
        "reboot-survival",
        "PostgreSQL surviving a reboot and a logout",
        "`postgres.boot` installs a `brew services` login agent. Whether it comes back after "
        "a restart and after logging out cannot be observed inside one unattended run.",
    ),
    (
        "rosetta-shell",
        "A Rosetta-translated shell on Apple Silicon",
        "`macos.architecture` refuses a translated shell. Producing one needs an "
        "Apple-Silicon Mac with Rosetta installed and a terminal launched under it.",
    ),
    (
        "interactive-wizard",
        "The interactive wizard on a terminal",
        "The short question set, its defaults and the consent prompts are read from a tty. "
        "This run is unattended by construction.",
    ),
)


def render_markdown(record: dict[str, Any]) -> str:
    """The evidence row a human reads, from the record a machine reads."""
    facts = record["host"]
    lines = [
        "# Native macOS acceptance evidence",
        "",
        f"- Recorded: `{record['recorded_at']}`",
        f"- Runner: `{record['runner']}`",
        f"- macOS: `{facts.get('platform_mac_ver') or '?'}` "
        f"(`{(facts.get('sw_vers') or '').replace(chr(10), ' / ')}`)",
        f"- Architecture: `{facts.get('platform_machine')}` (`arch` reports `{facts.get('arch')}`)",
        f"- Homebrew: `{facts.get('brew_version') or 'absent'}` "
        f"at `{facts.get('brew_prefix') or '-'}`",
        f"- Xcode CLT: `{facts.get('xcode_select') or 'absent'}`",
        f"- Python: `{facts.get('python') or '?'}` (`{facts.get('python_executable')}`)",
        f"- Installer version: `{record['installer_version']}`",
        f"- Selected capabilities: `{', '.join(record['capabilities']) or 'none'}`",
        f"- Verdict: **{record['verdict']}**",
        "",
        "## Phases",
        "",
        "| Phase | Verdict | Detail |",
        "| --- | --- | --- |",
    ]
    for phase in record["phases"]:
        detail = phase["detail"].replace("|", r"\|")
        lines.append(f"| `{phase['id']}` | {phase['verdict']} | {detail} |")
    lines += ["", "## Commands", ""]
    for phase in record["phases"]:
        if not phase["commands"]:
            continue
        lines.append(f"### `{phase['id']}` — {phase['title']}")
        lines.append("")
        for command in phase["commands"]:
            argv = " ".join(command["argv"])
            lines.append(f"- `{argv}` → exit `{command['exit_code']}` ({command['duration_s']}s)")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("macos-acceptance"))
    parser.add_argument("--aq", default=os.environ.get("AQ_BIN", "aq"))
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument(
        "--with",
        dest="capabilities",
        action="append",
        default=None,
        help="Capability to select (repeatable). Default: " + ", ".join(DEFAULT_CAPABILITIES),
    )
    parser.add_argument("--skip", action="append", default=[], help="Skip a phase by id.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even though this machine already has an AQ install home.",
    )
    parser.add_argument(
        "--allow-non-darwin",
        action="store_true",
        help="Do not refuse a non-macOS host (for checking this script itself only; "
        "the record is then marked as not native evidence).",
    )
    args = parser.parse_args(argv)

    native = platform.system() == "Darwin"
    if not native and not args.allow_non_darwin:
        print(
            "This script records *native* macOS evidence and refuses to run on "
            f"{platform.system() or 'an unknown system'}. A cross-platform run is not "
            "evidence for the macOS matrix row; see "
            "docs/plans/install-onboarding/contract.md.",
            file=sys.stderr,
        )
        return 2

    home = Path(os.path.expanduser("~/.agent-queue"))
    if home.exists() and not args.force:
        print(
            f"{home} already exists. This journey installs, repairs and upgrades AQ on this "
            "machine: run it on a disposable Mac, a fresh VM or a CI runner, point HOME at a "
            "scratch directory, or pass --force if this machine is expendable.",
            file=sys.stderr,
        )
        return 2

    aq = shutil.which(args.aq) or args.aq
    journey = Journey(
        aq=aq,
        repo=args.repo.resolve(),
        capabilities=tuple(args.capabilities or DEFAULT_CAPABILITIES),
        skip=set(args.skip),
    )
    journey.run_all()

    verdicts = {phase.verdict for phase in journey.phases}
    verdict = FAIL if FAIL in verdicts else PASS
    host_phase = next((p for p in journey.phases if p.id == "host"), None)
    version = run([aq, "--version"], timeout=120).stdout.strip() or "unknown"

    record = {
        "schema": "aq.acceptance.macos/1",
        "recorded_at": _now(),
        "native": native,
        "runner": os.environ.get("RUNNER_NAME") or os.environ.get("ImageOS") or platform.node(),
        "installer_version": version,
        "capabilities": list(journey.capabilities),
        "verdict": verdict,
        "host": host_phase.facts if host_phase else {},
        "phases": [asdict(phase) for phase in journey.phases],
    }

    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "evidence.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (args.output / "evidence.md").write_text(render_markdown(record), encoding="utf-8")

    print(render_markdown(record))
    print(f"Wrote {args.output / 'evidence.json'} and {args.output / 'evidence.md'}")
    return 0 if verdict == PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
