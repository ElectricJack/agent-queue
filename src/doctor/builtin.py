"""The generic built-in doctor checks (design §5.2).

Everything here is *generic*: config, database, vault, binaries, logs, event
registry, MCP probes.  Subsystem-specific checks (sessions, tmux, worktrees,
leases) are contributed by their owners — see
:data:`src.doctor.models.RESERVED_CHECK_IDS` for the registration contract.

Each check is read-only unless it declares a ``fix``, and every fix here is
idempotent and touches only derived state (WAL file, expired log directories).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

logger = logging.getLogger(__name__)

_MB = 1024 * 1024


# ---------------------------------------------------------------------------
# config.parse
# ---------------------------------------------------------------------------


async def _check_config_parse(ctx: DoctorContext) -> CheckResult:
    """Re-load the config file from disk and re-run ``AppConfig.validate()``."""
    from src.config import ConfigValidationError, load_config

    path = getattr(ctx.config, "_config_path", "") or ""
    if not path or not os.path.exists(path):
        return CheckResult(
            id="config.parse",
            severity=Severity.INFO,
            detail="no config file on disk (running from defaults)",
            data={"path": path},
        )

    def _load():
        return load_config(path, profile=getattr(ctx.config, "profile", "") or None)

    try:
        fresh = await asyncio.to_thread(_load)
    except ConfigValidationError as exc:
        return CheckResult(
            id="config.parse",
            severity=Severity.ERROR,
            detail=f"{path} failed validation: {exc}",
            data={"path": path},
        )
    except Exception as exc:
        return CheckResult(
            id="config.parse",
            severity=Severity.ERROR,
            detail=f"{path} could not be loaded: {type(exc).__name__}: {exc}",
            data={"path": path},
        )

    errors = [str(e) for e in fresh.validate()]
    if errors:
        return CheckResult(
            id="config.parse",
            severity=Severity.WARN,
            detail=f"{len(errors)} validation warning(s): {errors[0]}",
            data={"path": path, "errors": errors},
        )
    return CheckResult(
        id="config.parse",
        severity=Severity.OK,
        detail=f"{path} parses and validates cleanly",
        data={"path": path},
    )


# ---------------------------------------------------------------------------
# db.connect / db.migrations / db.wal_size
# ---------------------------------------------------------------------------


async def _check_db_connect(ctx: DoctorContext) -> CheckResult:
    """Same trivial probe the ``/health`` endpoint uses."""
    if ctx.db is None:
        return CheckResult(
            id="db.connect", severity=Severity.ERROR, detail="no database handle configured"
        )
    try:
        agents = await ctx.db.list_agents()
    except Exception as exc:
        return CheckResult(
            id="db.connect",
            severity=Severity.ERROR,
            detail=f"database unreachable: {type(exc).__name__}: {exc}",
        )
    return CheckResult(
        id="db.connect",
        severity=Severity.OK,
        detail=f"database reachable ({len(agents)} agent rows)",
    )


def _alembic_head() -> str | None:
    """Return the head revision of the shipped Alembic script directory."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    project_root = Path(__file__).resolve().parent.parent.parent
    cfg = Config(str(project_root / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    return heads[0] if heads else None


async def _check_db_migrations(ctx: DoctorContext) -> CheckResult:
    """Compare ``alembic_version`` against the script directory head."""
    from sqlalchemy import text

    engine = getattr(ctx.db, "_engine", None) if ctx.db is not None else None
    if engine is None:
        return CheckResult(
            id="db.migrations",
            severity=Severity.INFO,
            detail="database not initialised — migration state unknown",
        )
    try:
        head = await asyncio.to_thread(_alembic_head)
    except Exception as exc:
        return CheckResult(
            id="db.migrations",
            severity=Severity.ERROR,
            detail=f"could not read alembic script directory: {type(exc).__name__}: {exc}",
        )

    try:
        async with engine.begin() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            rows = result.fetchall()
    except Exception as exc:
        return CheckResult(
            id="db.migrations",
            severity=Severity.ERROR,
            detail=(
                "alembic_version is unreadable "
                f"({type(exc).__name__}: {exc}) — run: alembic upgrade head"
            ),
            data={"head": head},
        )

    current = rows[0][0] if rows else None
    if current == head:
        return CheckResult(
            id="db.migrations",
            severity=Severity.OK,
            detail=f"schema at head ({head})",
            data={"head": head, "current": current},
        )
    return CheckResult(
        id="db.migrations",
        severity=Severity.ERROR,
        detail=(
            f"schema at {current or 'unstamped'}, head is {head} — run: alembic upgrade head"
        ),
        data={"head": head, "current": current},
    )


def _sqlite_path(ctx: DoctorContext) -> str | None:
    """Resolve the on-disk SQLite file, or None for non-SQLite installs."""
    if getattr(ctx.config.database, "backend", "sqlite") != "sqlite":
        return None
    path = getattr(ctx.db, "_path", None) if ctx.db is not None else None
    if not path:
        path = ctx.config.database.url or ctx.config.database_path
    if not path or path == ":memory:":
        return None
    return path


async def _check_db_wal_size(ctx: DoctorContext) -> CheckResult:
    """Warn when the SQLite write-ahead log has grown past the threshold."""
    path = _sqlite_path(ctx)
    if path is None:
        return CheckResult(
            id="db.wal_size",
            severity=Severity.INFO,
            detail="not a file-backed SQLite database — WAL size not applicable",
        )
    wal = f"{path}-wal"
    if not os.path.exists(wal):
        return CheckResult(
            id="db.wal_size", severity=Severity.OK, detail="no WAL file present"
        )
    size_mb = os.path.getsize(wal) / _MB
    threshold = ctx.config.security.wal_warn_mb
    data = {"path": wal, "size_mb": round(size_mb, 1), "threshold_mb": threshold}
    if size_mb > threshold:
        return CheckResult(
            id="db.wal_size",
            severity=Severity.WARN,
            detail=f"WAL is {size_mb:.0f} MB (threshold {threshold} MB)",
            fixable=True,
            data=data,
        )
    return CheckResult(
        id="db.wal_size",
        severity=Severity.OK,
        detail=f"WAL is {size_mb:.1f} MB (threshold {threshold} MB)",
        fixable=True,
        data=data,
    )


async def _fix_db_wal_size(ctx: DoctorContext) -> CheckResult:
    """Checkpoint and truncate the WAL.  Idempotent — a no-op when already small."""
    from sqlalchemy import text

    engine = getattr(ctx.db, "_engine", None) if ctx.db is not None else None
    if engine is None:
        return CheckResult(
            id="db.wal_size", severity=Severity.INFO, detail="no engine — nothing to checkpoint"
        )
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    return CheckResult(id="db.wal_size", severity=Severity.OK, detail="WAL checkpointed")


# ---------------------------------------------------------------------------
# vault.parse
# ---------------------------------------------------------------------------


def _scan_vault(vault_root: str) -> tuple[list[str], int]:
    """Parse every operator-authored vault file; return (errors, files_seen)."""
    from src.profiles.mcp_registry import derive_server_id, parse_server_markdown
    from src.profiles.parser import parse_profile
    from src.profiles.workspace_kind_parser import parse_workspace_kind_file

    errors: list[str] = []
    seen = 0
    root = Path(vault_root)
    if not root.is_dir():
        return errors, seen

    for profile_path in sorted(root.glob("**/agent-types/*/profile.md")):
        seen += 1
        try:
            parsed = parse_profile(profile_path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"{profile_path}: {type(exc).__name__}: {exc}")
            continue
        if not parsed.is_valid:
            errors.append(f"{profile_path}: {'; '.join(parsed.errors)}")

    for kind_path in sorted(root.glob("**/workspace-kinds/*.md")):
        seen += 1
        try:
            parse_workspace_kind_file(kind_path, "system")
        except Exception as exc:
            errors.append(f"{kind_path}: {exc}")

    for mcp_path in sorted(root.glob("**/mcp-servers/*.md")):
        seen += 1
        rel = mcp_path.relative_to(root).as_posix()
        derived = derive_server_id(rel)
        try:
            parsed_mcp = parse_server_markdown(
                mcp_path.read_text(encoding="utf-8"),
                fallback_name=derived[1] if derived else mcp_path.stem,
                project_id=derived[0] if derived else None,
            )
        except Exception as exc:
            errors.append(f"{mcp_path}: {type(exc).__name__}: {exc}")
            continue
        if not parsed_mcp.is_valid:
            errors.append(f"{mcp_path}: {'; '.join(parsed_mcp.errors)}")

    return errors, seen


async def _check_vault_parse(ctx: DoctorContext) -> CheckResult:
    """Parse profiles, workspace kinds and MCP server files in report-only mode."""
    vault_root = os.path.join(ctx.config.data_dir, "vault")
    if not os.path.isdir(vault_root):
        return CheckResult(
            id="vault.parse",
            severity=Severity.INFO,
            detail=f"no vault directory at {vault_root}",
        )
    errors, seen = await asyncio.to_thread(_scan_vault, vault_root)
    if errors:
        return CheckResult(
            id="vault.parse",
            severity=Severity.ERROR,
            detail=f"{len(errors)} of {seen} vault file(s) failed to parse: {errors[0]}",
            data={"files": errors, "scanned": seen},
        )
    return CheckResult(
        id="vault.parse",
        severity=Severity.OK,
        detail=f"{seen} vault file(s) parse cleanly",
        data={"scanned": seen},
    )


# ---------------------------------------------------------------------------
# harness.binaries
# ---------------------------------------------------------------------------


#: Seconds a single ``<binary> --version`` probe may take.  Module-level so
#: tests can shorten it instead of sleeping through the real value.
_PROBE_TIMEOUT_S = 5.0


async def _terminate(proc) -> None:
    """Kill and reap *proc*, never raising.  No-op when it already exited."""
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
    except (ProcessLookupError, OSError):  # pragma: no cover - race
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2)
    except Exception:  # pragma: no cover - best effort; CancelledError propagates
        pass


async def _probe_binary(name: str) -> tuple[str, bool, str]:
    """Return ``(name, ok, detail)`` for one binary, never raising."""
    path = shutil.which(name)
    if not path:
        return name, False, "not on PATH"
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            path,
            "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=_PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        # Cancelling communicate() abandons the child; kill it and reap so a
        # doctor run never leaves a stuck `--version` process behind.
        await _terminate(proc)
        return name, False, "--version timed out"
    except Exception as exc:
        await _terminate(proc)
        return name, False, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return name, False, f"--version exited {proc.returncode}"
    version = (stdout.decode(errors="replace").strip().splitlines() or [""])[0]
    return name, True, version


async def _check_harness_binaries(ctx: DoctorContext) -> CheckResult:
    """Probe the binaries the shipped runtimes and git integration need.

    NARROWING (design §5.2 says "per configured harness"): the list is fixed,
    not derived from the profiles in the vault.  Deriving it means resolving
    every active profile's ``runtime`` / ``agent_name`` to a binary, and the
    profile→binary mapping for the 14+ ACP agents lives in ``acpx``, not
    here.  What is probed instead is the union the daemon can name on its own:
    ``git`` (required — every workspace operation goes through it) plus the
    two runtime front-ends and the forge CLI, each optional because an
    install that uses one does not need the others.  Recorded in
    ``docs/gates/wave1-1c-trust-ops.md`` rather than silently narrowed.
    """
    required = ["git"]
    # ``claude`` backs the default claude_sdk runtime; ``acpx`` backs the
    # fan-out runtime; ``gh`` is used for PR creation.
    optional = ["gh", "claude", "acpx"]
    # ``tmux`` is deliberately absent — session-runtime contributes tmux.server.
    results = await asyncio.gather(
        *(_probe_binary(n) for n in required + optional)
    )
    found = {name: (ok, detail) for name, ok, detail in results}
    missing_required = [n for n in required if not found[n][0]]
    missing_optional = [n for n in optional if not found[n][0]]
    data = {"binaries": {n: {"ok": ok, "detail": d} for n, (ok, d) in found.items()}}
    if missing_required:
        return CheckResult(
            id="harness.binaries",
            severity=Severity.ERROR,
            detail=f"required binaries missing: {', '.join(missing_required)}",
            data=data,
        )
    if missing_optional:
        return CheckResult(
            id="harness.binaries",
            severity=Severity.WARN,
            detail=f"optional binaries missing: {', '.join(missing_optional)}",
            data=data,
        )
    return CheckResult(
        id="harness.binaries",
        severity=Severity.OK,
        detail=f"{len(found)} binaries respond",
        data=data,
    )


# ---------------------------------------------------------------------------
# logs.llm_size
# ---------------------------------------------------------------------------


def _llm_log_dir(ctx: DoctorContext) -> str:
    return os.path.join(ctx.config.data_dir, "logs", "llm")


def _walk_llm_logs(base: str, retention_days: int) -> tuple[float, list[str]]:
    """Return (total MB, list of date dirs beyond retention)."""
    from datetime import datetime, timedelta, timezone

    total = 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    stale: list[str] = []
    for root, _dirs, files in os.walk(base):
        for fname in files:
            try:
                total += os.path.getsize(os.path.join(root, fname))
            except OSError:
                pass
    for name in sorted(os.listdir(base)):
        if os.path.isdir(os.path.join(base, name)) and len(name) == 10 and name < cutoff:
            stale.append(name)
    return total / _MB, stale


async def _check_logs_llm_size(ctx: DoctorContext) -> CheckResult:
    """Warn on an oversized ``logs/llm/`` tree or date dirs past retention."""
    base = _llm_log_dir(ctx)
    if not os.path.isdir(base):
        return CheckResult(
            id="logs.llm_size", severity=Severity.OK, detail="no LLM log directory"
        )
    retention = ctx.config.llm_logging.retention_days
    size_mb, stale = await asyncio.to_thread(_walk_llm_logs, base, retention)
    threshold = ctx.config.security.llm_log_warn_mb
    data = {
        "path": base,
        "size_mb": round(size_mb, 1),
        "threshold_mb": threshold,
        "beyond_retention": stale,
        "retention_days": retention,
    }
    if size_mb > threshold or stale:
        parts = []
        if size_mb > threshold:
            parts.append(f"{size_mb:.0f} MB (threshold {threshold} MB)")
        if stale:
            parts.append(f"{len(stale)} dir(s) past {retention}-day retention")
        return CheckResult(
            id="logs.llm_size",
            severity=Severity.WARN,
            detail="LLM logs: " + "; ".join(parts),
            fixable=True,
            data=data,
        )
    return CheckResult(
        id="logs.llm_size",
        severity=Severity.OK,
        detail=f"LLM logs {size_mb:.1f} MB, nothing past retention",
        fixable=True,
        data=data,
    )


async def _fix_logs_llm_size(ctx: DoctorContext) -> CheckResult:
    """Enforce the configured retention.  Never deletes inside the window."""
    from src.llm_logger import LLMLogger

    base = _llm_log_dir(ctx)
    llm_logger = LLMLogger(
        base_dir=base,
        enabled=ctx.config.llm_logging.enabled,
        retention_days=ctx.config.llm_logging.retention_days,
    )
    removed = await asyncio.to_thread(llm_logger.cleanup_old_logs)
    return CheckResult(
        id="logs.llm_size",
        severity=Severity.OK,
        detail=f"removed {removed} log dir(s) past retention",
        data={"removed": removed},
    )


# ---------------------------------------------------------------------------
# tasks.stuck / pauses.active
# ---------------------------------------------------------------------------


async def _check_tasks_stuck(ctx: DoctorContext) -> CheckResult:
    """Delegate to ``_cmd_get_stuck_tasks`` rather than re-deriving the rule."""
    if ctx.handler is None:
        return CheckResult(
            id="tasks.stuck",
            severity=Severity.INFO,
            detail="no command handler available — stuck-task scan skipped",
        )
    threshold = ctx.config.monitoring.stuck_task_threshold_seconds
    result = await ctx.handler._cmd_get_stuck_tasks(
        {
            "assigned_threshold_seconds": threshold,
            "in_progress_threshold_seconds": threshold,
            "now": time.time(),
        }
    )
    if "error" in result:
        return CheckResult(
            id="tasks.stuck", severity=Severity.ERROR, detail=str(result["error"])
        )
    stuck = result.get("stuck", [])
    if stuck:
        return CheckResult(
            id="tasks.stuck",
            severity=Severity.WARN,
            detail=f"{len(stuck)} task(s) stuck beyond {threshold}s",
            data={"stuck": stuck, "threshold_seconds": threshold},
        )
    return CheckResult(
        id="tasks.stuck",
        severity=Severity.OK,
        detail=f"no tasks stuck beyond {threshold}s",
        data={"threshold_seconds": threshold},
    )


async def _check_pauses_active(ctx: DoctorContext) -> CheckResult:
    """Report intentionally paused subsystems.  Always INFO — pauses are a choice."""
    orchestrator = getattr(ctx.handler, "orchestrator", None)
    paused = {
        "memory": not ctx.config.memory.enabled,
        "playbooks": not ctx.config.playbooks.enabled,
        "orchestrator_scheduling": bool(getattr(orchestrator, "_paused", False)),
    }
    active = [name for name, is_paused in paused.items() if is_paused]
    detail = (
        f"paused: {', '.join(sorted(active))}" if active else "no subsystems paused"
    )
    return CheckResult(
        id="pauses.active", severity=Severity.INFO, detail=detail, data=paused
    )


# ---------------------------------------------------------------------------
# events.registry
# ---------------------------------------------------------------------------


async def _check_events_registry(ctx: DoctorContext) -> CheckResult:
    """Flag *observed* event types with no registered payload schema.

    Reads :attr:`src.event_bus.EventBus.seen_event_types` — the set of types
    the live bus has actually dispatched — so the result reflects this
    daemon's real traffic.  A freshly started daemon has emitted nothing yet;
    that reports INFO ("nothing observed"), never OK: "no problems found" and
    "nothing was looked at" are different answers and must not read alike.

    The complementary *static* half — every literal ``.emit("…")`` in ``src/``
    has a registered schema — is enforced at test time by
    ``tests/test_event_schema_registry_validation.py``.
    """
    from src.event_schemas import registered_event_types

    registered = set(registered_event_types())

    emitted: set[str] = set()
    orchestrator = getattr(ctx.handler, "orchestrator", None)
    bus = getattr(orchestrator, "bus", None)
    seen = getattr(bus, "seen_event_types", None)
    if seen:
        emitted |= set(seen)
    plugin_registry = getattr(orchestrator, "plugin_registry", None)
    plugin_types = getattr(plugin_registry, "_event_types", None)
    if plugin_types:
        emitted |= set(plugin_types)

    unregistered = sorted(t for t in emitted if t not in registered)
    if unregistered:
        return CheckResult(
            id="events.registry",
            severity=Severity.WARN,
            detail=f"{len(unregistered)} emitted event type(s) have no schema: "
            + ", ".join(unregistered[:5]),
            data={
                "unregistered": unregistered,
                "registered_count": len(registered),
                "observed_count": len(emitted),
            },
        )
    if not emitted:
        return CheckResult(
            id="events.registry",
            severity=Severity.INFO,
            detail=(
                f"{len(registered)} event types registered; nothing emitted yet "
                "on this daemon, so nothing was compared (static coverage is "
                "enforced by tests/test_event_schema_registry_validation.py)"
            ),
            data={"registered_count": len(registered), "observed_count": 0},
        )
    return CheckResult(
        id="events.registry",
        severity=Severity.OK,
        detail=(
            f"{len(emitted)} observed event type(s), all registered "
            f"({len(registered)} schemas)"
        ),
        data={"registered_count": len(registered), "observed_count": len(emitted)},
    )


# ---------------------------------------------------------------------------
# mcp.probes
# ---------------------------------------------------------------------------


async def _check_mcp_probes(ctx: DoctorContext) -> CheckResult:
    """Probe every MCP server declared in the vault (10 s budget)."""
    from src.profiles.mcp_registry import McpRegistry, load_from_vault
    from src.profiles.mcp_probe import probe_many

    vault_root = os.path.join(ctx.config.data_dir, "vault")
    registry = McpRegistry()
    load_errors = await asyncio.to_thread(load_from_vault, registry, vault_root)
    configs = registry.list_all()
    if not configs:
        return CheckResult(
            id="mcp.probes",
            severity=Severity.INFO,
            detail="no MCP servers registered",
            data={"load_errors": load_errors},
        )
    results = await probe_many(configs, timeout=10.0)
    failed = [r for r in results if not getattr(r, "ok", False)]
    data = {
        "probed": len(results),
        "failed": [
            {"server": getattr(r, "server_name", "?"), "error": getattr(r, "error", None)}
            for r in failed
        ],
        "load_errors": load_errors,
    }
    if failed:
        return CheckResult(
            id="mcp.probes",
            severity=Severity.WARN,
            detail=f"{len(failed)} of {len(results)} MCP server(s) did not respond",
            data=data,
        )
    return CheckResult(
        id="mcp.probes",
        severity=Severity.OK,
        detail=f"all {len(results)} MCP server(s) responded",
        data=data,
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def builtin_checks() -> list[DoctorCheck]:
    """Every generic check doctor ships with."""
    return [
        DoctorCheck(id="config.parse", run=_check_config_parse),
        DoctorCheck(id="db.connect", run=_check_db_connect),
        DoctorCheck(id="db.migrations", run=_check_db_migrations),
        DoctorCheck(
            id="db.wal_size", run=_check_db_wal_size, fix=_fix_db_wal_size
        ),
        DoctorCheck(id="vault.parse", run=_check_vault_parse, timeout_s=15.0),
        DoctorCheck(id="harness.binaries", run=_check_harness_binaries, timeout_s=15.0),
        DoctorCheck(
            id="logs.llm_size", run=_check_logs_llm_size, fix=_fix_logs_llm_size, timeout_s=15.0
        ),
        DoctorCheck(id="tasks.stuck", run=_check_tasks_stuck),
        DoctorCheck(id="pauses.active", run=_check_pauses_active),
        DoctorCheck(id="events.registry", run=_check_events_registry),
        DoctorCheck(id="mcp.probes", run=_check_mcp_probes, timeout_s=15.0),
    ]
