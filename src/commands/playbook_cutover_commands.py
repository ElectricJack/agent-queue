"""Operator commands for the Playbook V1 → V2 cutover: drain, switch, window.

Playbook V2 Package 7 §3.3
(``docs/superpowers/plans/2026-09-01-playbook-v2-cutover-cleanup.md``).

Ten commands, all operator-only and all exempt from
``PAUSED_PLAYBOOK_COMMANDS``.  That exemption is the one place this package
widens a surface, and it is deliberate: ``playbooks.enabled`` defaults to
``False``, and a fleet that paused the subsystem with runs still ``running``
must still be able to see and clear them.  Draining is exactly the operation
you need when the subsystem is off.

Seven of them are the §3.3 drain, switch and window commands.  The other three
are the §3.9 human gates the switch refuses without:

* ``playbook_cutover_gate_status`` — read-only: the readiness table, the
  current G1 sign-off and the G2 authorizations, and what still blocks;
* ``playbook_cutover_drain_signoff`` — **G1**: a named human attests that the
  drain is complete *after* the command has re-verified every readiness check
  itself, recorded as ``drain_completed``;
* ``playbook_cutover_authorize`` — **G2**: one named human authorizes the
  switch in one role (``author`` or ``release_operator``), recorded as
  ``cutover_authorized`` and bound to the G1 row it authorizes.  The switch
  needs both roles, signed by two different people.

The human's name is an explicit ``signed_by`` attestation, stored in the
event's ``detail``, alongside — not instead of — the server-derived ``actor``.
The loopback CLI carries no user identity, so the two-person rule is enforced
on the attested names; the audit row records both what the server knew and
what the human declared.  A sign-off is scoped to one cutover attempt: a
rollback, a re-opened admission or a completed switch each start a new attempt
and the paperwork from the last one does not carry over
(:data:`~src.playbooks.cutover.CYCLE_BOUNDARY_EVENT_KINDS`).

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
    CUTOVER_AUTHORIZATION_ROLES,
    MIN_CUTOVER_REASON_LENGTH,
    MIN_SIGNER_LENGTH,
    DrainStatus,
    authorization_status,
    current_drain_signoff,
    display_signer,
    drain_status,
    normalize_signer,
    playbook_runtime,
    readiness_check,
    v1_admission_closed,
    v1_latency_baseline,
)

logger = logging.getLogger(__name__)

#: Exact operator-facing error text; the CLI and the dashboard match on it.
REASON_TOO_SHORT_ERROR = (
    f"a cutover reason must be at least {MIN_CUTOVER_REASON_LENGTH} characters — "
    "an unexplained cutover write is not auditable"
)

#: Exact operator-facing error text for a gate write with no attested human.
SIGNER_REQUIRED_ERROR = (
    f"signed_by is required — the name of the human attesting to this gate, at least "
    f"{MIN_SIGNER_LENGTH} characters; a gate nobody signed is not a gate"
)

#: How many audit rows the gate evaluation reads.  A cutover leaves a handful;
#: the bound exists so a pathological table cannot stall the switch.
_GATE_EVENT_LIMIT: int = 10_000

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

    @staticmethod
    def _cutover_signer(args: dict) -> tuple[str, dict | None]:
        """``(signed_by, error)`` — the attesting human's name, validated.

        This is the one request-body field a gate write records about
        identity, and it is recorded *as an attestation*, next to the
        server-derived ``actor`` — never in its place.
        """
        raw = args.get("signed_by")
        if not normalize_signer(raw):
            return "", {"success": False, "error": SIGNER_REQUIRED_ERROR}
        return display_signer(raw), None

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
            rows = await lister(limit=_GATE_EVENT_LIMIT)
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

    async def _cutover_gate_events(self) -> list[dict[str, Any]]:
        return await self.db.list_playbook_cutover_events(limit=_GATE_EVENT_LIMIT)

    # ------------------------------------------------------------------
    # Gates G1 and G2 (§3.9)
    # ------------------------------------------------------------------

    async def _cmd_playbook_cutover_gate_status(self, args: dict) -> dict:
        """Where the switch stands: readiness, the G1 sign-off, the G2 signatures.

        Read-only and recomputed from source on every call, like the window
        status — a gate that trusts a stored verdict is not a gate.
        ``can_switch`` is true only when every readiness check passes, a
        current drain sign-off exists and both authorization roles are signed
        by two different people.
        """
        now = time.time()
        checks, blocking, _drain = await self._cutover_readiness()
        events = await self._cutover_gate_events()
        signoff = current_drain_signoff(events)
        auth = authorization_status(signoff, events)
        reasons = list(blocking) + list(auth.blocking_reasons)
        runtime = playbook_runtime(self.config)
        return {
            "success": True,
            "generated_at": now,
            "runtime": runtime,
            "ready": not blocking,
            "checks": checks,
            "drain_signoff": signoff,
            "authorizations": [dict(a) for a in auth.authorizations],
            "blocking_reasons": reasons,
            "can_switch": not reasons and runtime == "v1",
        }

    async def _cmd_playbook_cutover_drain_signoff(self, args: dict) -> dict:
        """Gate G1: a named human signs off the drain — after the command checks it.

        Refuses while any readiness check blocks, naming each one; refuses a
        second sign-off for the same attempt, because a fresh sign-off would
        orphan every authorization bound to the first.  Records
        ``drain_completed`` with the attested name, the readiness table it
        verified and the V1 latency baseline the §3.5 gates are anchored to.

        Args:
            reason: Required — at least 10 chars.
            signed_by: Required — the attesting human's name.
        """
        reason, error = self._cutover_reason(args)
        if error:
            return error
        signer, error = self._cutover_signer(args)
        if error:
            return error

        checks, blocking, _drain = await self._cutover_readiness()
        if blocking:
            return {
                "success": False,
                "error": (
                    f"refusing to sign off the drain: {len(blocking)} readiness check(s) block"
                ),
                "checks": checks,
                "blocking_reasons": blocking,
            }

        existing = current_drain_signoff(await self._cutover_gate_events())
        if existing is not None:
            signed_by = (existing.get("detail") or {}).get("signed_by") or existing["actor"]
            return {
                "success": False,
                "error": (
                    f"the drain was already signed off at {existing['at']} by {signed_by} "
                    f"(event {existing['event_id']}); authorize the switch with "
                    "playbook_cutover_authorize"
                ),
                "checks": checks,
                "blocking_reasons": [],
            }

        runs = await self.db.list_playbook_runs(limit=1000)
        event = await self.db.append_playbook_cutover_event(
            kind="drain_completed",
            actor=self._cutover_actor(),
            reason=reason,
            detail={
                "signed_by": signer,
                "checks": checks,
                "v1_baseline": v1_latency_baseline(runs),
            },
        )
        logger.warning(
            "Playbook V1 drain signed off (G1) by %s, attested by %s: %s",
            event["actor"],
            signer,
            reason,
        )
        return {"success": True, "event": event, "checks": checks, "blocking_reasons": []}

    async def _cmd_playbook_cutover_authorize(self, args: dict) -> dict:
        """Gate G2: one named human authorizes the switch in one role.

        Bound to the current G1 sign-off; refused without one.  One signature
        per role and one role per person: the change author and the release
        operator must be two different people, compared on their normalised
        names.  Records ``cutover_authorized``.

        Args:
            reason: Required — at least 10 chars.
            signed_by: Required — the authorizing human's name.
            role: Required — ``author`` or ``release_operator``.
        """
        reason, error = self._cutover_reason(args)
        if error:
            return error
        signer, error = self._cutover_signer(args)
        if error:
            return error
        role = str(args.get("role") or "").strip()
        if role not in CUTOVER_AUTHORIZATION_ROLES:
            return {
                "success": False,
                "error": "role must be one of " + ", ".join(CUTOVER_AUTHORIZATION_ROLES),
            }

        events = await self._cutover_gate_events()
        signoff = current_drain_signoff(events)
        if signoff is None:
            return {
                "success": False,
                "error": (
                    "no current drain sign-off (G1) to authorize; run "
                    "playbook_cutover_drain_signoff first"
                ),
            }
        before = authorization_status(signoff, events)
        for existing in before.authorizations:
            if existing.get("role") == role:
                return {
                    "success": False,
                    "error": (
                        f"{role} already authorized by {existing['signed_by']} for drain "
                        f"sign-off {signoff['event_id']} (event {existing['event_id']})"
                    ),
                }
            if normalize_signer(existing.get("signed_by")) == normalize_signer(signer):
                return {
                    "success": False,
                    "error": (
                        f"G2 requires two distinct people: {existing['signed_by']} already "
                        f"authorized this sign-off as {existing['role']}"
                    ),
                }

        event = await self.db.append_playbook_cutover_event(
            kind="cutover_authorized",
            actor=self._cutover_actor(),
            reason=reason,
            detail={
                "role": role,
                "signed_by": signer,
                "drain_signoff_event_id": signoff["event_id"],
            },
        )
        logger.warning(
            "Playbook cutover authorized (G2, %s) by %s, attested by %s: %s",
            role,
            event["actor"],
            signer,
            reason,
        )
        after = authorization_status(signoff, events + [event])
        _checks, blocking, _drain = await self._cutover_readiness()
        reasons = list(blocking) + list(after.blocking_reasons)
        return {
            "success": True,
            "event": event,
            "drain_signoff_event_id": signoff["event_id"],
            "authorizations": [dict(a) for a in after.authorizations],
            "blocking_reasons": reasons,
            "can_switch": not reasons and playbook_runtime(self.config) == "v1",
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
        events), a current G1 drain sign-off, and G2 authorizations from both
        roles by two different people (§3.9).  The sign-off is evidence about
        the past; the switch checks the present, because a V1 run or a pending
        event that appeared after G1 is one the switch would strand.

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

            events = await self._cutover_gate_events()
            signoff = current_drain_signoff(events)
            auth = authorization_status(signoff, events)
            if signoff is None:
                return {
                    "success": False,
                    "error": (
                        "refusing to switch to v2: no current drain sign-off (G1) — "
                        "run playbook_cutover_drain_signoff"
                    ),
                    "checks": checks,
                    "blocking_reasons": list(auth.blocking_reasons),
                    **status.to_dict(),
                }
            if not auth.satisfied:
                return {
                    "success": False,
                    "error": (
                        "refusing to switch to v2: G2 authorization is incomplete — "
                        + "; ".join(auth.blocking_reasons)
                    ),
                    "checks": checks,
                    "blocking_reasons": list(auth.blocking_reasons),
                    "authorizations": [dict(a) for a in auth.authorizations],
                    **status.to_dict(),
                }
            detail.update(
                {
                    "drain_signoff_event_id": signoff["event_id"],
                    "authorizations": [dict(a) for a in auth.authorizations],
                    "checks": checks,
                }
            )

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
