"""Read-only reconciliation of execution registrations against live processes."""
from __future__ import annotations

import asyncio

from src.agents.sessions import flock_session_rows
from src.sessions.host_shell import is_host_shell_name
from src.sessions.proctable import read_environ_sync, scan_by_env_marker
from src.sessions.provider import PartialListError
from src.sessions.spec import SessionSpecBuilder


async def _instance_processes(config):
    processes = await scan_by_env_marker("AQ_INSTANCE_TOKEN")
    api_url = SessionSpecBuilder(config)._default_api_url()

    def select_instance():
        selected = []
        for process in processes:
            env = read_environ_sync(process.pid)
            if env is None:
                continue  # The process disappeared between probes.
            endpoint = env.get("AQ_API_URL")
            if endpoint is None or endpoint.rstrip("/") == api_url:
                selected.append(process)
        return selected

    # A disposable e2e daemon and the operator can share a host. A marked
    # process explicitly dialling another daemon belongs to that install.
    return await asyncio.to_thread(select_instance)


async def audit_flock(db, providers, config, *, scan_processes=True) -> list[dict]:
    """Report every hidden row/process and incomplete probe; never silently pass."""
    findings = []
    observed = []
    for name in providers.names():
        try:
            provider = providers.create(name, config)
            observed.extend(await provider.list_running(""))
        except PartialListError as exc:
            observed.extend(exc.partial)
            findings.append({"kind": "probe_failed", "provider": name,
                             "detail": "provider listing is incomplete"})
        except Exception:
            findings.append({"kind": "probe_failed", "provider": name,
                             "detail": "provider listing failed"})
    processes = await _instance_processes(config) if scan_processes else []
    # Read registrations after the observations: a concurrent launch must
    # commit its row before it can appear in either probe.
    sessions = await db.list_sessions()
    identities = {(row.provider, row.name, row.instance_token) for row in sessions}
    terminal = {
        (row.provider, row.name, row.instance_token): row
        for row in sessions if row.state in {"stopped", "quarantined"}
    }
    tokens = {row.instance_token for row in sessions if row.instance_token}
    terminal_tokens = {row.instance_token for row in terminal.values()} - {
        row.instance_token for row in sessions if row.state not in {"stopped", "quarantined"}
    }
    roster = flock_session_rows(sessions, await db.list_agents(), include_stopped=True)
    visible = {row["session_id"] for row in roster}
    for row in sessions:
        if row.id not in visible:
            findings.append({"kind": "row_hidden", "session_id": row.id,
                             "project_id": row.project_id,
                             "detail": f"session {row.name} is absent from the flock"})
    for handle in observed:
        if is_host_shell_name(handle.name):
            continue
        if not handle.instance_token and not handle.name.startswith(("s-", "n-", "p-")):
            continue  # An unrelated human tmux session is not an AQ agent.
        if (handle.provider, handle.name, handle.instance_token) not in identities:
            findings.append({"kind": "provider_untracked", "provider": handle.provider,
                             "name": handle.name,
                             "detail": f"live session {handle.name} has no flock registration"})
        elif (row := terminal.get((handle.provider, handle.name, handle.instance_token))):
            findings.append({"kind": "terminal_live", "session_id": row.id,
                             "project_id": row.project_id,
                             "detail": f"live session {handle.name} is hidden by stopped history"})
    untracked: dict[str, list[int]] = {}
    for process in processes:
        if process.marker and (process.marker not in tokens or process.marker in terminal_tokens):
            untracked.setdefault(process.marker, []).append(process.pid)
    for pids in untracked.values():
        findings.append({"kind": "process_untracked", "pids": sorted(pids),
                         "detail": "live AQ-marked processes have no active flock registration"})
    return findings
