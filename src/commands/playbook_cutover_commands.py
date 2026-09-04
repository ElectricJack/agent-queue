"""Operator commands for the Playbook V1 → V2 cutover: drain, switch, window.

Playbook V2 Package 7 §3.3
(``docs/superpowers/plans/2026-09-01-playbook-v2-cutover-cleanup.md``).

Nine mechanical commands, all operator-only and all exempt from
``PAUSED_PLAYBOOK_COMMANDS``.  That exemption is the one place this package
widens a surface, and it is deliberate: ``playbooks.enabled`` defaults to
``False``, and a fleet that paused the subsystem with runs still ``running``
must still be able to see and clear them.  Draining is exactly the operation
you need when the subsystem is off.

Cutover policy belongs in custom playbooks.  The core switch enforces only
mechanical readiness: V1 admission is closed, V1 runs are drained, migration
and rollback artifacts are ready, every enabled V2 activation is healthy, and
no V2 event is pending.  It re-verifies those facts at switch time and records
the evidence with the server-derived actor and reason in the append-only log.

Three compensations make the widening safe, each asserted in
``tests/test_playbook_cutover.py``:

* every write takes a mandatory ``reason`` of at least
  :data:`~src.playbooks.cutover.MIN_CUTOVER_REASON_LENGTH` characters, stored
  verbatim in the append-only audit table;
* every write appends to ``playbook_cutover_events`` *before* returning
  success, and there is no delete or update path;
* the ``actor`` is taken from the server-side execution principal, never from
  the request body — a switch that can name its own author is not attributable.

The rollback window (§3.5) is measured by :mod:`src.playbooks.cutover_window`
from evidence this module collects in :meth:`_cutover_window_evidence`; every
source is durable — audit rows, V2 run rows, receipts, waits, the events
table, the committed parity report — because the window is 72 hours long and
the daemon restarts inside it.  ``tests/test_playbook_cutover_window.py``
plants one failing source per measure and proves the close names it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from src.playbooks.cutover import (
    CANCEL_JOIN_TIMEOUT,
    MIN_CUTOVER_REASON_LENGTH,
    DrainStatus,
    drain_status,
    playbook_runtime,
    readiness_check,
    v1_admission_closed,
    v1_latency_baseline,
)
from src.playbooks.cutover_window import (
    PENDING_EVENT_REASONS,
    WINDOW_MIN_SECONDS,
    WINDOW_MIN_V2_RUNS,
    WindowEvidence,
    evaluate_window,
)

logger = logging.getLogger(__name__)

#: Exact operator-facing error text; the CLI and the dashboard match on it.
REASON_TOO_SHORT_ERROR = (
    f"a cutover reason must be at least {MIN_CUTOVER_REASON_LENGTH} characters — "
    "an unexplained cutover write is not auditable"
)

#: Bound pending-event reads so a pathological table cannot stall readiness.
_PENDING_EVENT_LIMIT: int = 10_000

#: How many live ``playbook_v2_graph`` calls measure 12's p95 is taken over.
GRAPH_PROBE_SAMPLES: int = 5

#: Upper bound on the audit rows one window read pulls from ``events``.
_WINDOW_EVENT_LIMIT: int = 10_000

__all__ = [
    "GRAPH_PROBE_SAMPLES",
    "REASON_TOO_SHORT_ERROR",
    "WINDOW_MIN_SECONDS",
    "WINDOW_MIN_V2_RUNS",
    "PlaybookCutoverCommandsMixin",
]


class PlaybookCutoverCommandsMixin:
    """Drain, switch and rollback-window commands mixed into CommandHandler."""

    # ------------------------------------------------------------------
    # Shared plumbing
    # ------------------------------------------------------------------

    @staticmethod
    def _cutover_actor() -> str:
        """The server's own answer to "who is doing this?".

        Never the request body: these are the highest-privilege operations in
        the playbook subsystem, and an actor a caller can choose is not an
        audit trail.
        """
        from src.commands.principal import current_principal

        principal = current_principal()
        if principal is None:
            return "operator"
        for field in ("identity", "subject", "name", "kind"):
            value = getattr(principal, field, None)
            if isinstance(value, str) and value:
                return value
            if value is not None and not isinstance(value, str):
                return str(getattr(value, "value", value))
        return "operator"

    @staticmethod
    def _cutover_reason(args: dict) -> tuple[str, dict | None]:
        """``(reason, error)`` — the mandatory justification, validated."""
        reason = args.get("reason") or ""
        if not isinstance(reason, str) or len(reason.strip()) < MIN_CUTOVER_REASON_LENGTH:
            return "", {"success": False, "error": REASON_TOO_SHORT_ERROR}
        return reason.strip(), None

    def _cutover_manager(self) -> Any:
        """The live ``PlaybookManager``, or ``None``.

        ``None`` is a meaningful answer here rather than an error: the drain
        classifies every row as orphaned when it cannot prove a coroutine owns
        it, which is the safe default for a gate (§3.2).
        """
        return getattr(getattr(self, "orchestrator", None), "playbook_manager", None)

    async def _cutover_drain(self) -> Any:
        return await drain_status(
            db=self.db,
            manager=self._cutover_manager(),
            config=self.config,
            clock=time.time,
        )

    def _cutover_config_path(self) -> str | None:
        for holder in (self.config, getattr(self, "orchestrator", None)):
            config = getattr(holder, "config", holder)
            path = getattr(config, "_config_path", None)
            if path:
                return str(path)
        return None

    async def _cutover_write_playbooks_field(self, field: str, value: Any) -> dict | None:
        """Persist one ``playbooks:`` field, then apply it in memory.

        Returns an error dict on failure, ``None`` on success.  The candidate
        document is validated by :func:`src.config.load_config` on a temp file
        before the swap — the same path ``update_config`` uses — so an
        incoherent pair (``v2_engine`` on with admission open, §3.4) is refused
        here rather than at the next daemon boot.

        The in-memory update is what makes the change take effect for the
        entry-point guards without a restart; a file write alone would leave
        the running daemon still admitting V1 runs after the operator was told
        admission was closed.
        """
        import os
        import tempfile

        import yaml as _yaml

        from src.config import load_config
        from src.config_editor import read_raw_config, write_section

        path = self._cutover_config_path()
        if not path or not os.path.exists(path):
            return {"success": False, "error": "No config file path is set on this daemon."}

        raw = read_raw_config(path)
        section = dict(raw.get("playbooks") or {})
        section[field] = value
        candidate = dict(raw)
        candidate["playbooks"] = section

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, dir=os.path.dirname(path)
        ) as tmp:
            tmp_path = tmp.name
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                _yaml.safe_dump(candidate, handle)
            try:
                load_config(tmp_path)
            except Exception as exc:
                return {"success": False, "error": f"refused: {exc}"}
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:  # pragma: no cover - best-effort cleanup
                pass

        write_section(path, "playbooks", section)
        playbooks = getattr(self.config, "playbooks", None)
        if playbooks is not None:
            try:
                setattr(playbooks, field, value)
            except Exception:  # pragma: no cover - frozen config in a test double
                logger.warning("Could not apply playbooks.%s in memory", field)
        return None

    # ------------------------------------------------------------------
    # Drain
    # ------------------------------------------------------------------

    async def _cmd_playbook_v1_drain_status(self, args: dict) -> dict:
        """Every non-terminal V1 run, classified live vs orphaned.

        Read-only.  ``drained`` is true only when admission is closed *and* no
        active run remains — a zero count on its own is a snapshot, not a gate.
        """
        status = await self._cutover_drain()
        return {"success": True, **status.to_dict()}

    async def _cmd_playbook_v1_admission_close(self, args: dict) -> dict:
        """Refuse new V1 runs.  Already-paused V1 runs stay resumable.

        Args:
            reason: Required — why admission is closing, at least 10 chars.
        """
        reason, error = self._cutover_reason(args)
        if error:
            return error
        if v1_admission_closed(self.config):
            return {"success": False, "error": "v1 admission is already closed"}

        failure = await self._cutover_write_playbooks_field("v1_admission", "closed")
        if failure:
            return failure

        status = await self._cutover_drain()
        event = await self.db.append_playbook_cutover_event(
            kind="v1_admission_closed",
            actor=self._cutover_actor(),
            reason=reason,
            detail={
                "live_count": status.live_count,
                "orphaned_count": status.orphaned_count,
            },
        )
        logger.info(
            "V1 playbook admission closed by %s (%d live, %d orphaned): %s",
            event["actor"],
            status.live_count,
            status.orphaned_count,
            reason,
        )
        return {"success": True, "event": event, **status.to_dict()}

    async def _cmd_playbook_v1_admission_open(self, args: dict) -> dict:
        """Re-open V1 admission.  Legal only while the fleet is still on V1.

        Args:
            reason: Required — why admission is re-opening, at least 10 chars.
        """
        reason, error = self._cutover_reason(args)
        if error:
            return error
        if not v1_admission_closed(self.config):
            return {"success": False, "error": "v1 admission is already open"}
        if playbook_runtime(self.config) == "v2":
            # ``runtime=v2`` with admission open describes nothing, and it would
            # let a rollback silently start new V1 runs against artifacts nobody
            # reviewed (§3.4, §4.3).
            return {
                "success": False,
                "error": (
                    "v1 admission cannot be re-opened while the fleet is on v2 — "
                    "roll back with playbook_cutover_switch --to v1 first"
                ),
            }

        failure = await self._cutover_write_playbooks_field("v1_admission", "open")
        if failure:
            return failure

        event = await self.db.append_playbook_cutover_event(
            kind="v1_admission_reopened",
            actor=self._cutover_actor(),
            reason=reason,
        )
        logger.warning("V1 playbook admission re-opened by %s: %s", event["actor"], reason)
        status = await self._cutover_drain()
        return {"success": True, "event": event, **status.to_dict()}

    async def _cmd_playbook_v1_run_cancel(self, args: dict) -> dict:
        """Cancel one V1 run — a cancel that actually cancels.

        ``cancel_playbook_run`` writes ``cancelled`` to the row but cannot
        signal the coroutine, so a live run finishes its current node and its
        next persistence write silently puts the status back to ``running``.
        A drain built on that would report zero active runs and then watch the
        count go back up, which is the worst failure a cutover gate can have,
        because it is silent.

        The fix is ordering: cancel the task and *await* it, then write the
        terminal row.  The coroutine is gone before the row is written, so
        nothing is left to overwrite it.  A task that will not stop within
        :data:`~src.playbooks.cutover.CANCEL_JOIN_TIMEOUT` leaves the row
        untouched and the command fails — a half-cancelled run must not be
        reported drained.

        Args:
            run_id: Required — the V1 run to cancel.
            reason: Required — why, at least 10 chars.
        """
        run_id = args.get("run_id") or ""
        if not run_id:
            return {"success": False, "error": "run_id is required"}
        reason, error = self._cutover_reason(args)
        if error:
            return error

        from src.playbooks.cutover import ACTIVE_RUN_STATUSES

        run = await self.db.get_playbook_run(run_id)
        if run is None:
            return {"success": False, "error": f"no playbook run with id {run_id!r}"}
        if run.status not in ACTIVE_RUN_STATUSES:
            return {
                "success": False,
                "error": f"run {run_id} is already terminal (status={run.status})",
            }

        manager = self._cutover_manager()
        task = None
        if manager is not None:
            task = getattr(manager, "_running", {}).get(run_id)
        ownership = "live" if task is not None else "orphaned"

        if task is not None:
            task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.gather(task, return_exceptions=True),
                    timeout=CANCEL_JOIN_TIMEOUT,
                )
            except TimeoutError:
                return {
                    "success": False,
                    "error": f"run did not stop within {CANCEL_JOIN_TIMEOUT:.0f}s",
                }

        now = time.time()
        await self.db.update_playbook_run(
            run_id,
            status="cancelled",
            completed_at=now,
            error=f"cancelled during v1 drain: {reason}",
        )
        await self._cutover_emit_cancelled(run, reason)
        logger.info("V1 run %s cancelled during drain (%s): %s", run_id, ownership, reason)
        return {
            "success": True,
            "run_id": run_id,
            "ownership": ownership,
            "status": "cancelled",
            "completed_at": now,
        }

    async def _cutover_emit_cancelled(self, run: Any, reason: str) -> None:
        """Announce the cancellation on the bus, best-effort.

        Never fatal: the terminal row is the durable outcome, and a drain that
        failed because a notification subscriber raised would leave the
        operator with a cancelled run reported as a failure.
        """
        bus = getattr(getattr(self, "orchestrator", None), "bus", None)
        if bus is None:
            return
        try:
            from src.notifications.events import PlaybookRunCancelledEvent

            await bus.publish(
                PlaybookRunCancelledEvent(
                    run_id=run.run_id,
                    playbook_id=run.playbook_id,
                    reason=f"v1 drain: {reason}",
                )
            )
        except Exception:
            logger.debug("Could not publish PlaybookRunCancelledEvent", exc_info=True)

    # ------------------------------------------------------------------
    # Readiness — the G1 preconditions, each re-verified from source
    # ------------------------------------------------------------------
    #
    # Four checks, one row each, all fail-closed: an evidence source that
    # cannot be read blocks, it is never treated as satisfied.  The three
    # non-drain checks are methods so a unit test without a vault can stub
    # them at the seam the real handler uses; the drain check reads the same
    # rows the drain command does.

    @staticmethod
    def _cutover_drain_check(status: DrainStatus) -> dict[str, Any]:
        return readiness_check(
            "drain",
            observed={
                "admission": status.admission,
                "live_count": status.live_count,
                "orphaned_count": status.orphaned_count,
            },
            passed=status.drained,
            blocking=(
                f"the v1 drain is not complete: admission={status.admission}, "
                f"{status.live_count} live and {status.orphaned_count} orphaned v1 run(s) remain"
            ),
        )

    async def _cutover_check_report(self) -> dict[str, Any]:
        """Package 6's ``playbook_cutover_report``: no blockers, rollback ready."""
        report_cmd = getattr(self, "_cmd_playbook_cutover_report", None)
        if report_cmd is None:
            return readiness_check(
                "cutover_report",
                observed=None,
                passed=False,
                blocking="playbook_cutover_report is not available on this handler",
            )
        try:
            report = await report_cmd({})
        except Exception as exc:
            logger.warning("cutover gate: playbook_cutover_report failed", exc_info=True)
            return readiness_check(
                "cutover_report",
                observed=None,
                passed=False,
                blocking=f"playbook_cutover_report failed: {exc}",
            )
        reasons = [str(r) for r in (report.get("blocking_reasons") or [])]
        rollback_ready = report.get("rollback_ready") is True
        succeeded = report.get("success") is True
        observed = {
            "cutover_eligible": report.get("cutover_eligible") is True,
            "rollback_ready": rollback_ready,
            "contract_fingerprint": report.get("contract_fingerprint"),
            "blocking_reasons": reasons,
        }
        if not succeeded:
            blocking = str(report.get("error") or "playbook_cutover_report did not succeed")
        elif reasons:
            blocking = "; ".join(reasons)
        elif not rollback_ready:
            blocking = "rollback artifacts are incomplete"
        else:
            blocking = None
        return readiness_check(
            "cutover_report",
            observed=observed,
            passed=succeeded and not reasons and rollback_ready,
            blocking=blocking,
        )

    async def _cutover_check_activations(self) -> dict[str, Any]:
        """Every enabled activation is ``ready`` against the live contracts.

        ``ready`` is computed by :func:`~src.playbooks.activation.load_activation_health`
        from the stored artifact and the *current* command and profile
        registries, so it already subsumes "carries the current contract
        fingerprint" — a drifted fingerprint reports ``stale_contract``.
        """
        storage_ready = getattr(self, "_v2_activation_storage_ready", None)
        if storage_ready is not None and not storage_ready():
            return readiness_check(
                "activations",
                observed=None,
                passed=False,
                blocking="playbook V2 activation storage is not enabled on this daemon",
            )
        lookups = getattr(self, "_v2_lookups", None)
        if lookups is None or not hasattr(self.db, "list_playbook_activations"):
            return readiness_check(
                "activations",
                observed=None,
                passed=False,
                blocking="activation health cannot be read on this handler",
            )
        try:
            from src.playbooks.activation import ActivationHealth, load_activation_health

            contracts, profiles, _events = await lookups()
            records = await load_activation_health(
                self.db, contracts=contracts, profiles=profiles, enabled_only=True
            )
        except Exception as exc:
            logger.warning("cutover gate: activation health unavailable", exc_info=True)
            return readiness_check(
                "activations",
                observed=None,
                passed=False,
                blocking=f"activation health unavailable: {exc}",
            )
        not_ready = [
            f"{r.playbook_id}[{r.scope}:{r.scope_identifier}]={r.health.value}"
            for r in records
            if r.health is not ActivationHealth.READY
        ]
        observed = {
            "enabled": len(records),
            "ready": len(records) - len(not_ready),
            "not_ready": not_ready,
        }
        if not records:
            blocking = "no enabled activation — a v2 fleet with nothing activated runs nothing"
        elif not_ready:
            blocking = (
                f"{len(not_ready)} enabled activation(s) are not ready against the current "
                f"contract: {', '.join(not_ready)}"
            )
        else:
            blocking = None
        return readiness_check(
            "activations",
            observed=observed,
            passed=bool(records) and not not_ready,
            blocking=blocking,
        )

    async def _cutover_check_pending_events(self) -> dict[str, Any]:
        """Zero unresolved pending V2 events (§3.5 measure 14 at the switch)."""
        lister = getattr(self.db, "list_pending_events", None)
        if lister is None:
            return readiness_check(
                "pending_events",
                observed=None,
                passed=False,
                blocking="pending events cannot be read on this handler",
            )
        try:
            rows = await lister(limit=_PENDING_EVENT_LIMIT)
        except Exception as exc:
            logger.warning("cutover gate: pending events unavailable", exc_info=True)
            return readiness_check(
                "pending_events",
                observed=None,
                passed=False,
                blocking=f"pending events unavailable: {exc}",
            )
        count = len(rows)
        return readiness_check(
            "pending_events",
            observed={"unresolved": count},
            passed=count == 0,
            blocking=f"{count} unresolved pending event(s) require an operator decision",
        )

    async def _cutover_readiness(self) -> tuple[list[dict[str, Any]], list[str], DrainStatus]:
        """``(checks, blocking_reasons, drain)`` — the whole table, from source."""
        drain = await self._cutover_drain()
        checks = [
            self._cutover_drain_check(drain),
            await self._cutover_check_report(),
            await self._cutover_check_activations(),
            await self._cutover_check_pending_events(),
        ]
        blocking = [f"{row['check']}: {row['blocking']}" for row in checks if not row["pass"]]
        return checks, blocking, drain

    async def _cmd_playbook_cutover_gate_status(self, args: dict) -> dict:
        """Where the switch stands, based only on mechanical readiness.

        Read-only and recomputed from source on every call, like the window
        status. Approval policy belongs in custom playbooks, not this command.
        """
        now = time.time()
        checks, blocking, _drain = await self._cutover_readiness()
        runtime = playbook_runtime(self.config)
        return {
            "success": True,
            "generated_at": now,
            "runtime": runtime,
            "ready": not blocking,
            "checks": checks,
            "blocking_reasons": list(blocking),
            "can_switch": not blocking and runtime == "v1",
        }

    # ------------------------------------------------------------------
    # Switch and rollback window
    # ------------------------------------------------------------------

    async def _cmd_playbook_cutover_switch(self, args: dict) -> dict:
        """Move the fleet between the V1 and V2 runtimes.

        The highest-privilege operation in the package.  Switching to ``v2``
        requires, re-verified at the moment of the switch: every readiness
        check passing (the drain complete, the cutover report clean and
        rollback-ready, every enabled activation ``ready``, zero pending
        events). Any approval or review policy belongs in a custom playbook;
        the core only proves the runtime transition is mechanically safe.

        Switching back to ``v1`` is the rollback, and it needs no gate: an
        operator must be able to roll back at 3am.  It is legal only until the
        rollback window is closed: after that a ``rollback_window_closed`` row
        exists and recovery is a forward change, matching the roadmap's
        rollback boundary.

        Args:
            to: Required — ``v1`` or ``v2``.
            reason: Required — at least 10 chars.
        """
        target = args.get("to") or ""
        if target not in ("v1", "v2"):
            return {"success": False, "error": "to must be one of v1, v2"}
        reason, error = self._cutover_reason(args)
        if error:
            return error

        current = playbook_runtime(self.config)
        if current == target:
            return {"success": False, "error": f"the fleet is already on {target}"}

        closed = await self.db.latest_playbook_cutover_event("rollback_window_closed")
        if closed is not None and target == "v1":
            return {
                "success": False,
                "error": (
                    f"rollback window closed at {closed['at']}; "
                    "rollback now requires a forward change"
                ),
            }

        detail: dict[str, Any] = {"from": current, "to": target}
        if target == "v2":
            checks, blocking, status = await self._cutover_readiness()
            if not status.drained:
                return {
                    "success": False,
                    "error": (
                        "refusing to switch to v2 before the drain completes: "
                        f"admission={status.admission}, "
                        f"{status.live_count} live and {status.orphaned_count} "
                        "orphaned v1 run(s) remain"
                    ),
                    "checks": checks,
                    "blocking_reasons": blocking,
                    **status.to_dict(),
                }
            if blocking:
                return {
                    "success": False,
                    "error": (
                        f"refusing to switch to v2: {len(blocking)} readiness check(s) block"
                    ),
                    "checks": checks,
                    "blocking_reasons": blocking,
                    **status.to_dict(),
                }

            runs = await self.db.list_playbook_runs(limit=1000)
            detail.update({"checks": checks, "v1_baseline": v1_latency_baseline(runs)})

        failure = await self._cutover_write_playbooks_field("v2_engine", target == "v2")
        if failure:
            return failure

        event = await self.db.append_playbook_cutover_event(
            kind="switched_to_v2" if target == "v2" else "rolled_back_to_v1",
            actor=self._cutover_actor(),
            reason=reason,
            detail=detail,
        )
        logger.warning(
            "Playbook runtime switched %s -> %s by %s: %s", current, target, event["actor"], reason
        )
        return {"success": True, "runtime": target, "event": event}

    # ------------------------------------------------------------------
    # Rollback window — evidence collection
    # ------------------------------------------------------------------

    def _cutover_clock(self) -> float:
        return time.time()

    def _cutover_parity_path(self) -> Path:
        """The committed shadow-parity record (Package 6), repository-relative."""
        from src.playbooks.migration import REVIEWED_FIXTURE_ROOT

        return Path(REVIEWED_FIXTURE_ROOT) / "parity-report.json"

    async def _cutover_probe_graph_latency_ms(
        self, playbook_id: str, *, samples: int = GRAPH_PROBE_SAMPLES
    ) -> list[float]:
        """Time ``playbook_v2_graph`` against one playbook's active artifact.

        Live probes rather than a stored series: measure 12 is a property of
        the artifact the fleet is running *now*, and a gate that read last
        week's number would not notice a recompiled artifact.
        """
        graph = getattr(self, "_cmd_playbook_v2_graph", None)
        if graph is None:
            raise RuntimeError("playbook_v2_graph is not available on this handler")
        latencies: list[float] = []
        for _ in range(samples):
            started = time.perf_counter()
            result = await graph({"playbook_id": playbook_id, "include_advanced": False})
            elapsed = (time.perf_counter() - started) * 1000.0
            if isinstance(result, dict) and result.get("error"):
                raise RuntimeError(str(result["error"]))
            latencies.append(elapsed)
        return latencies

    async def _cutover_window_evidence(
        self, *, now: float, switched_at: float | None, drain: Any | None,
        drain_error: str | None,
    ) -> WindowEvidence:
        """Read every §3.5 source over ``[switched_at, now]``, failing closed.

        A source that raises is recorded under its key in ``errors`` and its
        value left ``None``; :func:`evaluate_window` turns that into a
        blocking row for every measure the source feeds.  Nothing here
        substitutes a default for a read that did not happen.
        """
        since = switched_at if switched_at is not None else 0.0
        errors: dict[str, str] = {}

        async def read(key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
            try:
                return await factory()
            except Exception as exc:
                logger.warning("cutover window: %s unreadable", key, exc_info=True)
                errors[key] = str(exc) or exc.__class__.__name__
                return None

        async def parity() -> dict[str, Any]:
            path = self._cutover_parity_path()
            record = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
            if not isinstance(record, dict):
                raise TypeError(f"{path}: not an object")
            return record

        async def audit_rows(event_type: str) -> list[dict[str, Any]]:
            rows = await self.db.get_recent_events(
                limit=_WINDOW_EVENT_LIMIT, event_type=event_type, since=since
            )
            decoded: list[dict[str, Any]] = []
            for row in rows:
                raw = row.get("payload") or "{}"
                try:
                    payload = json.loads(raw) if isinstance(raw, str) else dict(raw)
                except (TypeError, ValueError):
                    payload = {"_unparsed": raw}
                if not isinstance(payload, dict):
                    payload = {"_unparsed": raw}
                payload.setdefault("at", row.get("timestamp"))
                decoded.append(payload)
            return decoded

        async def baseline() -> dict[str, Any] | None:
            switched = await self.db.latest_playbook_cutover_event("switched_to_v2")
            if switched is None:
                return None
            recorded = switched.get("detail", {}).get("v1_baseline")
            return dict(recorded) if isinstance(recorded, dict) else None

        async def rehearsal() -> dict[str, Any] | None:
            event = await self.db.latest_playbook_cutover_event("window_coverage_rehearsal")
            if event is None:
                return None
            detail = event.get("detail", {})
            return {
                "at": event["at"],
                "actor": event["actor"],
                "playbooks": list(detail.get("playbooks") or []),
                "dashboard_tti_ms": detail.get("dashboard_tti_ms"),
            }

        parity_record = await read("parity", parity)
        activations = await read(
            "activations",
            lambda: self.db.list_playbook_activations_with_artifacts(enabled_only=True),
        )
        v2_runs = await read("v2_runs", lambda: self.db.count_v2_runs_by_playbook(since))
        denials = await read("denials", lambda: audit_rows("capability.denied"))
        conflicts = await read("conflicts", lambda: audit_rows("playbook.snapshot_conflict"))
        dispatch = await read(
            "dispatch_latency", lambda: self.db.v2_dispatch_latencies_ms(since)
        )
        resume = await read("resume_latency", lambda: self.db.wait_resume_latencies_ms(since))
        v1_baseline = await read("v1_baseline", baseline)
        receipts = await read("receipts", lambda: self.db.count_step_receipts_since(since))
        orphans = await read("waits", lambda: self.db.agent_task_wait_orphans(now))
        cancellations = await read(
            "cancellations", lambda: self.db.agent_task_cancellations_since(since)
        )
        pending = await read(
            "pending", lambda: self.db.pending_event_summary(reasons=PENDING_EVENT_REASONS)
        )
        rehearsed = await read("rehearsal", rehearsal)

        graph_target: str | None = None
        graph_latencies: list[float] | None = None
        if activations:
            # The largest enabled artifact, so the gate does not quietly
            # weaken as a small playbook happens to sort first.
            largest = max(
                activations,
                key=lambda row: (int(row.get("size_bytes") or 0), str(row.get("playbook_id"))),
            )
            graph_target = str(largest.get("playbook_id"))
            graph_latencies = await read(
                "graph", lambda: self._cutover_probe_graph_latency_ms(graph_target)
            )

        dashboard_tti = None
        if rehearsed and rehearsed.get("dashboard_tti_ms") is not None:
            dashboard_tti = {
                "ms": float(rehearsed["dashboard_tti_ms"]),
                "recorded_at": rehearsed["at"],
                "actor": rehearsed["actor"],
            }
        if drain_error is not None:
            errors["v1_runs"] = drain_error

        return WindowEvidence(
            now=now,
            switched_at=switched_at,
            parity=parity_record,
            enabled_activations=activations,
            v2_runs_by_playbook=v2_runs,
            denials=denials,
            conflicts=conflicts,
            dispatch_latencies_ms=dispatch,
            resume_latencies_ms=resume,
            v1_baseline=v1_baseline,
            step_counts=receipts,
            agent_task_orphans=orphans,
            agent_task_cancellations=cancellations,
            graph_latencies_ms=graph_latencies,
            graph_target=graph_target,
            dashboard_tti=dashboard_tti,
            pending=pending,
            active_v1_runs=len(drain.active) if drain is not None else None,
            rehearsal=rehearsed,
            errors=errors,
        )

    async def _cutover_window_verdict(self) -> dict[str, Any]:
        """The §3.5 table and the window, recomputed from source right now."""
        now = self._cutover_clock()
        switched = await self.db.latest_playbook_cutover_event("switched_to_v2")
        closed = await self.db.latest_playbook_cutover_event("rollback_window_closed")
        runtime = playbook_runtime(self.config)

        drain = None
        drain_error: str | None = None
        try:
            drain = await self._cutover_drain()
        except Exception as exc:
            logger.warning("cutover window: v1 drain unreadable", exc_info=True)
            drain_error = str(exc) or exc.__class__.__name__

        evidence = await self._cutover_window_evidence(
            now=now,
            switched_at=switched["at"] if switched else None,
            drain=drain,
            drain_error=drain_error,
        )
        verdict = evaluate_window(evidence)
        blocking = list(verdict.blocking_reasons)

        # A runtime that disagrees with the audit log is a hand-edited config.
        # By design an operator can roll back at 3am without a gate row, so
        # this is detected rather than prevented (§3.9).
        expected_runtime = "v2" if switched is not None else "v1"
        rolled_back = await self.db.latest_playbook_cutover_event("rolled_back_to_v1")
        if rolled_back is not None and switched is not None and rolled_back["at"] > switched["at"]:
            expected_runtime = "v1"
        if runtime != expected_runtime:
            blocking.append("runtime flipped outside the cutover command")

        window = {**verdict.window, "closed_at": closed["at"] if closed else None}
        return {
            "success": True,
            "generated_at": now,
            "runtime": runtime,
            "admission": drain.admission if drain is not None else None,
            "measures": verdict.measures,
            "window": window,
            "blocking_reasons": blocking,
            "evidence_errors": verdict.evidence_errors,
            "can_close": not blocking and closed is None,
        }

    # ------------------------------------------------------------------
    # Rollback window — commands
    # ------------------------------------------------------------------

    async def _cmd_playbook_cutover_window_status(self, args: dict) -> dict:
        """The §3.5 acceptance table, measured, plus the observation window.

        Read-only, and it recomputes from source every time — nothing here
        reads a cached verdict, because a gate that trusts a stored ``pass``
        is not a gate.  Every row names its source, what was observed and
        when; a source that could not be read is reported as ``evidence
        unreadable`` and fails the measures it feeds.  "Not measured" is
        never rendered as "fine".
        """
        return await self._cutover_window_verdict()

    async def _cmd_playbook_cutover_window_close(self, args: dict) -> dict:
        """Close the rollback window.  Refuses unless every gate passes.

        After this the V1 runtime may be deleted and rollback is no longer
        available, so the command recomputes §3.5 itself rather than reading a
        stored verdict, and it names every measure and window condition that
        stands in the way.  There is deliberately no ``--force``: an operator
        who wants to close early edits the config themselves and owns it, and
        the audit table records that they did not use the gate.

        Args:
            reason: Required — at least 10 chars.  Name the rehearsal when the
                coverage came from synthetic traffic.
        """
        reason, error = self._cutover_reason(args)
        if error:
            return error

        closed = await self.db.latest_playbook_cutover_event("rollback_window_closed")
        if closed is not None:
            return {
                "success": False,
                "error": f"the rollback window was already closed at {closed['at']}",
            }

        status = await self._cutover_window_verdict()
        blocking = status["blocking_reasons"]
        if blocking:
            return {
                "success": False,
                "error": f"window cannot close: {len(blocking)} blocking condition(s)",
                "blocking_reasons": blocking,
                "measures": status["measures"],
                "window": status["window"],
                "evidence_errors": status["evidence_errors"],
            }

        event = await self.db.append_playbook_cutover_event(
            kind="rollback_window_closed",
            actor=self._cutover_actor(),
            reason=reason,
            detail={
                "measures": status["measures"],
                "window": status["window"],
                "evidence_errors": status["evidence_errors"],
            },
        )
        logger.warning("Playbook rollback window closed by %s: %s", event["actor"], reason)
        return {
            "success": True,
            "event": event,
            "measures": status["measures"],
            "window": status["window"],
            "evidence_errors": status["evidence_errors"],
        }

    def _cutover_rehearsal_engine(self) -> Any:
        """The production V2 engine, built from the daemon's own dependencies."""
        from src.playbooks.services import build_v2_engine

        orchestrator = getattr(self, "orchestrator", None)
        return build_v2_engine(
            config=self.config,
            db=self.db,
            handler=self,
            llm=getattr(orchestrator, "llm", None),
            bus=getattr(orchestrator, "bus", None),
        )

    async def _cmd_playbook_cutover_window_rehearsal(self, args: dict) -> dict:
        """Dispatch one synthetic event per enabled playbook (§3.5 coverage).

        On an idle fleet the coverage condition — every enabled playbook has
        dispatched at least one V2 run since the switch — is satisfied by this
        rehearsal, which is recorded as a ``window_coverage_rehearsal`` audit
        row so a window closed on synthetic traffic says so.  Runs are live:
        a dry run writes no row and would prove nothing.  Coverage itself is
        still measured from the run table, never from this event.

        The rehearsal is also where the manual dashboard review (§3.5 measure
        13) is recorded, because it is the one operator-driven step of the
        window and the review is done alongside it.

        Args:
            reason: Required — at least 10 chars.
            dashboard_tti_ms: Optional — the semantic-tab time-to-interactive
                the operator measured in the manual scenario review.
        """
        reason, error = self._cutover_reason(args)
        if error:
            return error
        tti = args.get("dashboard_tti_ms")
        if tti is not None and (isinstance(tti, bool) or not isinstance(tti, (int, float))):
            return {
                "success": False,
                "error": "dashboard_tti_ms must be a number of milliseconds",
            }

        switched = await self.db.latest_playbook_cutover_event("switched_to_v2")
        if switched is None:
            return {
                "success": False,
                "error": (
                    "no switched_to_v2 event — run playbook_cutover_switch --to v2 before "
                    "rehearsing the window"
                ),
            }
        if playbook_runtime(self.config) != "v2":
            return {
                "success": False,
                "error": "the fleet is on v1; a rehearsal dispatches through the v2 engine",
            }

        from src.commands.principal import ExecutionPrincipal

        activations = await self.db.list_playbook_activations_with_artifacts(enabled_only=True)
        targets = sorted(
            (
                row
                for row in activations
                if str(getattr(row.get("health"), "value", row.get("health"))) == "ready"
                and (row.get("active_artifact_sha256") or row.get("artifact_sha256"))
            ),
            key=lambda row: (str(row.get("playbook_id")), str(row.get("scope_identifier") or "")),
        )
        engine = self._cutover_rehearsal_engine()
        store = engine.services.artifact_store
        principal = ExecutionPrincipal.service("playbook-cutover-rehearsal")
        now = self._cutover_clock()

        runs: dict[str, list[str]] = {}
        uncovered: list[str] = []
        errors: dict[str, str] = {}
        for row in targets:
            playbook_id = str(row.get("playbook_id"))
            sha = row.get("active_artifact_sha256") or row.get("artifact_sha256")
            run_ids = runs.setdefault(playbook_id, [])
            try:
                definition = store.load(sha)
            except Exception as exc:
                errors[playbook_id] = f"artifact {sha} unreadable: {exc}"
                continue
            for rule in definition.rules:
                event: dict[str, Any] = {
                    **dict(rule.trigger.filter or {}),
                    "event_id": f"rehearsal-{uuid.uuid4().hex[:12]}",
                    "type": rule.trigger.event_type,
                    "_event_type": rule.trigger.event_type,
                    "_rehearsal": True,
                    "_received_at": time.time(),
                }
                identifier = str(row.get("scope_identifier") or "")
                if row.get("scope") == "project" and identifier:
                    event["project_id"] = identifier
                elif row.get("scope") == "agent_type" and identifier:
                    event["agent_type"] = identifier
                try:
                    result = await engine.dispatch_event(
                        event, principal, playbook_ids={playbook_id}
                    )
                except Exception as exc:
                    errors[playbook_id] = f"rule {rule.id}: {exc.__class__.__name__}: {exc}"
                    continue
                run_ids.extend(str(run_id) for run_id in result.run_ids)
                if run_ids:
                    break  # one run is what coverage asks of each playbook
            if not run_ids:
                uncovered.append(playbook_id)

        detail: dict[str, Any] = {
            "playbooks": sorted(runs),
            "runs": runs,
            "uncovered": uncovered,
            "errors": errors,
            "recorded_at": now,
        }
        if tti is not None:
            detail["dashboard_tti_ms"] = float(tti)
        event = await self.db.append_playbook_cutover_event(
            kind="window_coverage_rehearsal",
            actor=self._cutover_actor(),
            reason=reason,
            detail=detail,
        )
        logger.info(
            "Playbook window coverage rehearsal by %s: %d playbook(s), %d uncovered: %s",
            event["actor"], len(runs), len(uncovered), reason,
        )
        return {
            "success": True,
            "event": event,
            "playbooks": sorted(runs),
            "runs": runs,
            "uncovered": uncovered,
            "errors": errors,
        }

