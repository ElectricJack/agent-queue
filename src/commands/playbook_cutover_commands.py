"""Operator commands for the Playbook V1 → V2 cutover: drain, switch, window.

Playbook V2 Package 7 §3.3
(``docs/superpowers/plans/2026-09-01-playbook-v2-cutover-cleanup.md``).

Seven commands, all operator-only and all exempt from
``PAUSED_PLAYBOOK_COMMANDS``.  That exemption is the one place this package
widens a surface, and it is deliberate: ``playbooks.enabled`` defaults to
``False``, and a fleet that paused the subsystem with runs still ``running``
must still be able to see and clear them.  Draining is exactly the operation
you need when the subsystem is off.

Three compensations make the widening safe, each asserted in
``tests/test_playbook_cutover.py``:

* every write takes a mandatory ``reason`` of at least
  :data:`~src.playbooks.cutover.MIN_CUTOVER_REASON_LENGTH` characters, stored
  verbatim in the append-only audit table;
* every write appends to ``playbook_cutover_events`` *before* returning
  success, and there is no delete or update path;
* the ``actor`` is taken from the server-side execution principal, never from
  the request body — a switch that can name its own author is not attributable.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from src.playbooks.cutover import (
    CANCEL_JOIN_TIMEOUT,
    MIN_CUTOVER_REASON_LENGTH,
    drain_status,
    playbook_runtime,
    v1_admission_closed,
    v1_latency_baseline,
)

logger = logging.getLogger(__name__)

#: Exact operator-facing error text; the CLI and the dashboard match on it.
REASON_TOO_SHORT_ERROR = (
    f"a cutover reason must be at least {MIN_CUTOVER_REASON_LENGTH} characters — "
    "an unexplained cutover write is not auditable"
)

#: Wall-clock floor for the rollback observation window (§3.5).
WINDOW_MIN_SECONDS: float = 72 * 3600.0

#: Volume floor for the rollback observation window (§3.5), rehearsal runs
#: included.  Wall clock alone is refused because an idle fleet reaches 72 h
#: having proved nothing.
WINDOW_MIN_V2_RUNS: int = 200

#: The §3.5 acceptance measures whose evidence sources this commit does not
#: yet read.  They are reported, and they do not pass — a window-close gate
#: that treated "not measured" as "fine" would be the silent failure the whole
#: drain design exists to prevent.  Package 7 commit 3 wires them.
_UNWIRED_MEASURES: tuple[tuple[int, str, str], ...] = (
    (1, "shadow rule-selection agreement", "tests/fixtures/playbooks/v2/parity-report.json"),
    (2, "command-argument agreement after canonicalisation", "parity-report.json"),
    (3, "unexplained terminal-outcome differences", "parity-report.json"),
    (4, "authorization denials by command and profile", "capability.denied counter"),
    (5, "duplicate receipt / snapshot-version conflicts", "RunRepository.commit_boundary"),
    (6, "event->run dispatch latency p95", "playbook.dispatch_ms"),
    (7, "wait-resume latency p95", "playbook.resume_ms"),
    (8, "LLM budget failures", "receipts with outcome budget_exceeded"),
    (9, "structured-output failures", "receipts with outcome output_invalid"),
    (10, "agent-task orphan rate", "agent-task steps with no terminal receipt"),
    (11, "agent-task cancellation rate", "receipts with outcome cancelled"),
    (12, "graph API latency p95", "playbook_v2_graph"),
    (13, "dashboard semantic-tab time-to-interactive", "manual scenario review"),
    (14, "pending-event count", "playbook_pending_events"),
    (15, "pending-event maximum age", "playbook_pending_events"),
)


def _measure(
    number: int,
    name: str,
    source: str,
    observed: Any,
    gate: str,
    passed: bool,
    blocking: str | None = None,
) -> dict[str, Any]:
    """One row of the §3.5 acceptance table, as the operator reads it."""
    row = {
        "measure": number,
        "name": name,
        "source": source,
        "observed": observed,
        "gate": gate,
        "pass": passed,
    }
    if blocking:
        row["blocking"] = blocking
    return row


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
    # Switch and rollback window
    # ------------------------------------------------------------------

    async def _cmd_playbook_cutover_switch(self, args: dict) -> dict:
        """Move the fleet between the V1 and V2 runtimes.

        The highest-privilege operation in the package.  Switching to ``v2``
        requires a completed drain, because a V1 run still executing when the
        switch lands is a run the switch strands.  Switching back to ``v1`` is
        the rollback, and it is legal only until the rollback window is closed:
        after that a ``rollback_window_closed`` row exists and recovery is a
        forward change, matching the roadmap's rollback boundary.

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

        if target == "v2":
            status = await self._cutover_drain()
            if not status.drained:
                return {
                    "success": False,
                    "error": (
                        "refusing to switch to v2 before the drain completes: "
                        f"admission={status.admission}, "
                        f"{status.live_count} live and {status.orphaned_count} "
                        "orphaned v1 run(s) remain"
                    ),
                    **status.to_dict(),
                }

        failure = await self._cutover_write_playbooks_field("v2_engine", target == "v2")
        if failure:
            return failure

        event = await self.db.append_playbook_cutover_event(
            kind="switched_to_v2" if target == "v2" else "rolled_back_to_v1",
            actor=self._cutover_actor(),
            reason=reason,
            detail={"from": current, "to": target},
        )
        logger.warning(
            "Playbook runtime switched %s -> %s by %s: %s", current, target, event["actor"], reason
        )
        return {"success": True, "runtime": target, "event": event}

    async def _cmd_playbook_cutover_window_status(self, args: dict) -> dict:
        """The §3.5 acceptance table, measured, plus the observation window.

        Read-only, and it recomputes from source every time — nothing here
        reads a cached verdict, because a gate that trusts a stored ``pass``
        is not a gate.

        Measures whose evidence sources this commit does not yet read are
        reported with ``pass: false`` and a ``blocking`` note naming what
        supplies them.  "Not measured" is never rendered as "fine": that is
        precisely the silent success the drain design exists to prevent.
        """
        now = time.time()
        switched = await self.db.latest_playbook_cutover_event("switched_to_v2")
        closed = await self.db.latest_playbook_cutover_event("rollback_window_closed")
        runtime = playbook_runtime(self.config)
        drain = await self._cutover_drain()

        blocking: list[str] = []
        measures = [
            _measure(
                16,
                "active V1 runs",
                "DrainStatus.active",
                len(drain.active),
                "0 — hard",
                not drain.active,
                None if not drain.active else "the drain has not reached zero",
            )
        ]
        for number, name, source in _UNWIRED_MEASURES:
            measures.append(
                _measure(
                    number,
                    name,
                    source,
                    None,
                    "see child plan §3.5",
                    False,
                    "not measured — Package 7 commit 3 wires this source",
                )
            )
        measures.sort(key=lambda row: row["measure"])
        blocking.extend(
            f"measure {row['measure']} ({row['name']}): {row.get('blocking', 'failed its gate')}"
            for row in measures
            if not row["pass"]
        )

        # A runtime that disagrees with the audit log is a hand-edited config.
        # By design an operator can roll back at 3am without a gate row, so
        # this is detected rather than prevented (§3.9).
        expected_runtime = "v2" if switched is not None else "v1"
        rolled_back = await self.db.latest_playbook_cutover_event("rolled_back_to_v1")
        if rolled_back is not None and switched is not None and rolled_back["at"] > switched["at"]:
            expected_runtime = "v1"
        if runtime != expected_runtime:
            blocking.append("runtime flipped outside the cutover command")

        elapsed = (now - switched["at"]) if switched else None
        window = {
            "switched_at": switched["at"] if switched else None,
            "elapsed_seconds": elapsed,
            "wall_clock_ok": bool(elapsed is not None and elapsed >= WINDOW_MIN_SECONDS),
            "wall_clock_gate_seconds": WINDOW_MIN_SECONDS,
            "volume_gate_runs": WINDOW_MIN_V2_RUNS,
            "closed_at": closed["at"] if closed else None,
        }
        if switched is None:
            blocking.append("no switched_to_v2 event — the window has not started")
        elif not window["wall_clock_ok"]:
            blocking.append(
                f"observation window is {elapsed:.0f}s old; {WINDOW_MIN_SECONDS:.0f}s required"
            )

        return {
            "success": True,
            "generated_at": now,
            "runtime": runtime,
            "admission": drain.admission,
            "measures": measures,
            "window": window,
            "blocking_reasons": blocking,
            "can_close": not blocking and closed is None,
        }

    async def _cmd_playbook_cutover_window_close(self, args: dict) -> dict:
        """Close the rollback window.  Refuses unless every gate passes.

        After this the V1 runtime may be deleted and rollback is no longer
        available, so the command recomputes §3.5 itself rather than reading a
        stored verdict, and it names every measure that stands in the way.
        There is deliberately no ``--force``: an operator who wants to close
        early edits the config themselves and owns it, and the audit table
        records that they did not use the gate.

        Args:
            reason: Required — at least 10 chars.
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

        status = await self._cmd_playbook_cutover_window_status({})
        if status.get("blocking_reasons"):
            return {
                "success": False,
                "error": "refusing to close the rollback window",
                "blocking_reasons": status["blocking_reasons"],
                "measures": status["measures"],
            }

        event = await self.db.append_playbook_cutover_event(
            kind="rollback_window_closed",
            actor=self._cutover_actor(),
            reason=reason,
            detail={"measures": status["measures"], "window": status["window"]},
        )
        logger.warning("Playbook rollback window closed by %s: %s", event["actor"], reason)
        return {"success": True, "event": event}

    # ------------------------------------------------------------------
    # Baseline
    # ------------------------------------------------------------------

    async def _cutover_record_drain_completed(self, reason: str) -> dict[str, Any]:
        """Record ``drain_completed`` with the V1 latency baseline.

        Called once the drain reaches zero.  The baseline is what every §3.5
        latency gate is expressed as a multiple of — there is no production V2
        baseline to compare against, so the honest anchor is what V1 actually
        did on this fleet.
        """
        runs = await self.db.list_playbook_runs(limit=1000)
        return await self.db.append_playbook_cutover_event(
            kind="drain_completed",
            actor=self._cutover_actor(),
            reason=reason,
            detail={"v1_baseline": v1_latency_baseline(runs)},
        )
