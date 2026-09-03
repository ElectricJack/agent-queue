"""Playbook V1 → V2 migration readiness commands.

Package 6 of the Playbook V2 roadmap
(``docs/superpowers/plans/2026-09-01-playbook-v2-migration-artifacts.md`` §3.6).
Three commands: one read-only inventory and two narrow operator writes against
the acknowledgement table.

**Nothing here activates, compiles or executes anything.**  ``build_inventory``
is read-only by contract, and the two writes touch one table whose only effect
is to move an entry from ``question_required`` to ``disabled`` in the report an
operator reads before cutting over.

The acknowledgement is a waiver, so it is deliberately narrow: one row per
playbook, no bulk form, no glob, no ``--force``, a 12-character floor on the
justification, and an ``acknowledged_by`` taken from the server-side
:class:`~src.commands.principal.ExecutionPrincipal` rather than from the request
body.  A request that supplies ``acknowledged_by`` has it ignored.
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.database.queries.playbook_migration_queries import MIN_ACK_REASON_LENGTH
from src.playbooks.migration import (
    REVIEWED_FIXTURE_ROOT,
    build_cutover_report,
    build_inventory,
    release_check,
    reviewed_artifact_evidence,
)

logger = logging.getLogger(__name__)

#: Exact operator-facing error text; the CLI and the dashboard match on it.
REASON_TOO_SHORT_ERROR = (
    f"an acknowledgement reason must be at least {MIN_ACK_REASON_LENGTH} characters — "
    "an empty waiver is not a waiver"
)


def _unread_evidence(source: str, exc: BaseException, detail: str = "") -> dict[str, str]:
    """One evidence source the cutover report could not read.

    Returned rather than swallowed: :func:`build_cutover_report` turns each of
    these into a blocking reason, so a failed query is never rendered as an
    empty — that is, clean — result.
    """
    return {"source": source, "error": f"{detail}{type(exc).__name__}: {exc}"}


def _active_sha(row: dict) -> str:
    """The artifact hash one activation row activates, or ``""``.

    ``playbook_activations`` stores the reference as ``active_artifact_sha256``;
    the joined read carries the artifact table's ``artifact_sha256`` alongside
    it.  Reading only the latter is what made every live activation invisible to
    the release check, so both names are accepted and the activation's own
    column is the authority.
    """
    if not isinstance(row, dict):
        return ""
    return str(row.get("active_artifact_sha256") or row.get("artifact_sha256") or "")


class PlaybookMigrationCommandsMixin:
    """Migration inventory and acknowledgement commands mixed into CommandHandler."""

    # ------------------------------------------------------------------
    # Shared plumbing
    # ------------------------------------------------------------------

    def _migration_vault_root(self) -> str:
        configured = getattr(self.config, "vault_root", None)
        if configured:
            return str(Path(configured).resolve())
        return str((Path(self.config.data_dir) / "vault").resolve())

    def _reviewed_fixture_root(self) -> Path:
        """Where the reviewed decision records and the parity record live.

        Repository-relative, deliberately: both are checked-in files, and the
        cutover report is produced from a checkout so that it can be signed
        against bytes a reviewer can read.  A daemon started somewhere else
        finds neither, and the report then blocks for missing review evidence
        instead of inventing approval it cannot see.
        """
        return Path(REVIEWED_FIXTURE_ROOT)

    def _migration_store(self):
        """The compiled V1 store, or ``None`` when the vault cannot be opened.

        A missing compiled tree is not an error: a fresh install has never
        compiled anything, and the inventory's job is to say so.
        """
        try:
            from src.playbooks.store import CompiledPlaybookStore
            from src.vault_manager import VaultManager

            return CompiledPlaybookStore(VaultManager(self.config))
        except Exception:  # pragma: no cover - defensive
            logger.warning("migration inventory: compiled store unavailable", exc_info=True)
            return None

    async def _migration_inventory(self):
        from src.commands.contracts import CONTRACTS

        return await build_inventory(
            vault_root=self._migration_vault_root(),
            store=self._migration_store(),
            contract_registry=CONTRACTS,
            activation_repo=self.db,
            ack_repo=self.db,
            pending_repo=self.db,
        )

    async def _enabled_activations(
        self, *, evidence_errors: list[dict[str, str]] | None = None
    ) -> list[dict]:
        """Enabled activation rows joined to the artifact each one activates.

        Both §5.5 reports need the artifact's identity — its hash and the
        source digest it was compiled from — and neither lives on the
        activation row.  A repository without the joined read degrades to the
        unjoined rows, which report less rather than reporting nothing.
        """
        try:
            rows = await self.db.list_playbook_activations_with_artifacts(enabled_only=True)
        except AttributeError:  # pragma: no cover - repositories predating the join
            try:
                rows = await self.db.list_playbook_activations()
            except Exception as exc:
                logger.warning("migration report: activation rows unavailable", exc_info=True)
                if evidence_errors is not None:
                    evidence_errors.append(_unread_evidence("activations", exc))
                return []
        except Exception as exc:  # pragma: no cover - defensive live-daemon reporting
            logger.warning("migration report: activation rows unavailable", exc_info=True)
            if evidence_errors is not None:
                evidence_errors.append(_unread_evidence("activations", exc))
            return []
        return [row for row in rows if isinstance(row, dict) and row.get("enabled", True)]

    @staticmethod
    def _migration_principal_identity() -> str:
        """The server's own answer to "who is acknowledging this?".

        Never the request body: a waiver that can name its own author is not
        attributable, and this is the one command in Package 6 that can move
        the fleet past a real problem.
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

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    async def _cmd_playbook_migration_inventory(self, args: dict) -> dict:
        """Report every installed playbook's V2 migration readiness.

        Read-only: it never compiles, activates or writes anything.

        Args:
            disposition: Optional filter — ``ready``, ``question_required``,
                ``invalid`` or ``disabled``.  The counts and the blocking
                total always describe the whole fleet, because a filtered
                report that also filtered its own summary would understate
                what stands between the operator and cutover.
        """
        disposition = args.get("disposition")
        inventory = await self._migration_inventory()
        payload = inventory.to_dict()
        if disposition:
            if disposition not in ("ready", "question_required", "invalid", "disabled"):
                return {
                    "success": False,
                    "error": (
                        f"unknown disposition {disposition!r}; expected one of "
                        "ready, question_required, invalid, disabled"
                    ),
                }
            payload["entries"] = [
                entry.to_dict() for entry in inventory.by_disposition(disposition)
            ]
            payload["filtered_by"] = disposition
        return {"success": True, **payload}

    async def _cmd_playbook_migration_acknowledge(self, args: dict) -> dict:
        """Record a written waiver that one playbook cannot migrate.

        The waiver binds to the source bytes present right now, so any later
        edit to the authoring Markdown invalidates it and the playbook returns
        to ``question_required``.  ``acknowledged_by`` is taken from the
        execution principal; a request that supplies one is ignored.

        Args:
            playbook_id: Required — the playbook's frontmatter id.
            reason: Required — why it cannot migrate, at least 12 characters.
        """
        playbook_id = args.get("playbook_id") or ""
        reason = args.get("reason") or ""
        if not playbook_id:
            return {"success": False, "error": "playbook_id is required"}
        if not isinstance(reason, str) or len(reason.strip()) < MIN_ACK_REASON_LENGTH:
            return {"success": False, "error": REASON_TOO_SHORT_ERROR}

        inventory = await self._migration_inventory()
        entry = next((e for e in inventory.entries if e.playbook_id == playbook_id), None)
        if entry is None:
            return {
                "success": False,
                "error": f"no playbook with id {playbook_id!r} is installed in this vault",
            }

        row = await self.db.upsert_playbook_migration_ack(
            playbook_id=entry.playbook_id,
            scope=entry.scope,
            scope_identifier=entry.scope_identifier or "",
            source_sha256=entry.source.source_sha256,
            reason=reason,
            acknowledged_by=self._migration_principal_identity(),
        )
        logger.info(
            "Playbook migration acknowledged: %s (%s) by %s",
            entry.playbook_id,
            entry.scope,
            row["acknowledged_by"],
        )
        return {"success": True, "acknowledgement": row}

    async def _cmd_playbook_migration_unacknowledge(self, args: dict) -> dict:
        """Withdraw a waiver; the entry returns to its computed disposition.

        Args:
            playbook_id: Required — the playbook's frontmatter id.
        """
        playbook_id = args.get("playbook_id") or ""
        if not playbook_id:
            return {"success": False, "error": "playbook_id is required"}
        rows = await self.db.list_playbook_migration_acks()
        matches = [row for row in rows if row.get("playbook_id") == playbook_id]
        if not matches:
            return {
                "success": False,
                "error": f"no acknowledgement is recorded for {playbook_id!r}",
            }
        removed = 0
        for row in matches:
            if await self.db.delete_playbook_migration_ack(
                playbook_id=row["playbook_id"],
                scope=row["scope"],
                scope_identifier=row.get("scope_identifier") or "",
            ):
                removed += 1
        return {"success": True, "playbook_id": playbook_id, "removed": removed}

    # ------------------------------------------------------------------
    # §5.5 — release check
    # ------------------------------------------------------------------

    async def _release_check_activations(
        self, *, evidence_errors: list[dict[str, str]] | None = None
    ) -> list[dict]:
        """Enabled activations, each with the artifact's per-command fingerprints.

        Every enabled activation produces a row.  One whose artifact cannot be
        loaded produces a row *without* command evidence, tagged with why, and
        :func:`release_check` names it in ``unverified`` rather than comparing
        it.  It used to be skipped here on the grounds that the activation
        health surface already calls it `unavailable`, but that made the release
        gate — whose entire claim is "every enabled activation was compared" —
        report a clean fleet whose activations it had never read
        (`prime-zenith-66`).  Naming the row costs one line in the report and
        keeps the claim true.

        Reads that fail outright — the activation query, the artifact store,
        the profile registry — are appended to *evidence_errors* in the same
        ``{"source", "error"}`` shape :func:`build_cutover_report` takes, and
        each becomes a blocking reason.

        Each comparable row also carries `current_profiles`, resolved from this
        daemon's *live* profile registry rather than from the shipped defaults.
        An activated artifact was compiled against the profiles in this
        database, operator edits included, so holding it to
        `src/profiles/defaults/` would report a legitimately customised profile
        as drift.  When that registry cannot be read the row is flagged
        ``current_profiles_unavailable`` instead, because falling back to the
        shipped defaults would compare the artifact against a baseline it was
        never compiled against.

        The waiver table is deliberately **not** joined here.  `release_check`
        excuses a disabled activation, never an acknowledged one: a waiver says
        a playbook is not migrating, an enabled activation says it already
        runs V2, and treating the waiver as authoritative over a live row
        suppressed the compatibility check for execution that is really
        happening (`sound-horizon-20`).  Every row this method emits is enabled
        — `_enabled_activations` filters on it — so every row is compared.  An
        operator who means a waiver disables the activation.
        """
        from src.playbooks.migration import profile_fingerprints_for

        errors = evidence_errors if evidence_errors is not None else []
        rows: list[dict] = []
        activations = await self._enabled_activations(evidence_errors=errors)

        def _row(source: dict, **extra) -> dict:
            return {
                "playbook_id": str(source.get("playbook_id") or ""),
                "enabled": True,
                "scope": str(source.get("scope") or "system"),
                "scope_identifier": source.get("scope_identifier"),
                "artifact_sha256": _active_sha(source) or None,
                **extra,
            }

        try:
            from src.playbooks.artifact_store import ArtifactStore

            store = ArtifactStore(
                self.config.compiled_root,
                max_artifact_bytes=self.config.playbooks.v2_max_artifact_bytes,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("release check: artifact store unavailable", exc_info=True)
            errors.append(_unread_evidence("artifact_store", exc))
            # Name the store once and every activation it hides: an operator
            # needs both the cause and the list of playbooks left unverified.
            return [
                _row(
                    row,
                    evidence_reason="artifact_store_unavailable",
                    evidence_detail=f"{type(exc).__name__}: {exc}",
                )
                for row in activations
            ]

        profile_lookup = None
        try:
            _contracts, profile_lookup, _events = await self._v2_lookups()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("release check: profile registry unavailable", exc_info=True)
            profile_lookup = None
            errors.append(_unread_evidence("profile_registry", exc))

        for row in activations:
            sha = _active_sha(row)
            if not sha:
                rows.append(
                    _row(
                        row,
                        evidence_reason="no_active_artifact",
                        evidence_detail="the enabled activation names no artifact",
                    )
                )
                continue
            try:
                definition = store.load(sha)
            except Exception as exc:
                logger.warning("release check: artifact %s unreadable", sha, exc_info=True)
                rows.append(
                    _row(
                        row,
                        evidence_reason="artifact_unreadable",
                        evidence_detail=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            artifact_profiles = dict(definition.compiled_against.profiles)
            entry = _row(
                row,
                artifact_commands=dict(definition.compiled_against.commands),
                artifact_profiles=artifact_profiles,
            )
            if profile_lookup is not None:
                entry["current_profiles"] = profile_fingerprints_for(
                    profile_lookup, artifact_profiles
                )
            else:
                entry["current_profiles_unavailable"] = True
            rows.append(entry)
        return rows

    async def _cmd_playbook_release_check(self, args: dict) -> dict:
        """Are the reviewed V2 artifacts still valid against the current contracts?

        Compares every checked-in reviewed fixture and every enabled activation
        against the in-process contract registry, and reports each command whose
        *execution* fingerprint moved since the artifact was reviewed.  A
        presentation-only label change does not appear here, because it does not
        enter the execution fingerprint.

        Offline by construction: no network, no LLM, no compile.  It is the same
        assertion `tests/test_playbook_contract_release_check.py` makes in CI,
        available against a live daemon.

        It fails **closed**.  Live evidence this daemon could not read is
        reported as ``evidence_errors`` and any enabled activation it could not
        compare as ``unverified``; both feed ``blocking_reasons``, and either
        one makes ``success`` false.  A gate that answered "yes" from an
        unreadable activation table would be worse than no gate.
        """
        from src.commands.contracts import CONTRACTS

        evidence_errors: list[dict[str, str]] = []
        try:
            activations = await self._release_check_activations(
                evidence_errors=evidence_errors
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("release check: activations unavailable", exc_info=True)
            activations = []
            evidence_errors.append(_unread_evidence("activations", exc))
        try:
            return release_check(
                contract_registry=CONTRACTS,
                fixture_root=self._reviewed_fixture_root(),
                activations=activations,
                evidence_errors=evidence_errors,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("release check failed", exc_info=True)
            failed = [*evidence_errors, _unread_evidence("release_check", exc)]
            return {
                "success": False,
                "stale": [],
                "checked": [],
                "unverified": [],
                "evidence_errors": failed,
                "blocking_reasons": [
                    f"evidence source {row['source']!r} could not be read ({row['error']}); "
                    "a release cannot be certified against evidence that was never collected"
                    for row in failed
                ],
                "error": str(exc),
            }

    async def _cutover_report_inputs(self) -> dict:
        """Collect report inputs without turning the report into an operation.

        A read that fails is recorded as an unavailable evidence source, never
        as an empty result: an empty ``pending_events`` list means "the fleet
        has no pending events", and a failed query that returned one would let
        the report certify a fleet nobody looked at.  Every recorded failure
        becomes a blocking reason in :func:`build_cutover_report`, so the
        report fails closed.
        """
        from src.commands.contracts import CONTRACTS

        evidence_errors: list[dict[str, str]] = []

        fixture_root = self._reviewed_fixture_root()
        inventory = await self._migration_inventory()
        enabled = await self._enabled_activations(evidence_errors=evidence_errors)

        store = self._migration_store()
        try:
            v1_ids = {
                str(getattr(playbook, "id", "") or "")
                for _scope, _identifier, playbook in (store.list_all() if store is not None else [])
            }
        except Exception as exc:  # pragma: no cover - read-only fallback
            logger.warning("cutover report: V1 store unavailable", exc_info=True)
            evidence_errors.append(_unread_evidence("v1_store", exc))
            v1_ids = set()

        # ``reviewed_by``/``reviewed_at`` exist only in the human decision
        # records (§3.4), and a review is evidence about *specific bytes*: one
        # that names a different artifact hash than the row activates is
        # evidence that the live artifact was never reviewed, so it is dropped
        # rather than reported as approval of whatever is running now.
        reviews = reviewed_artifact_evidence(fixture_root)
        artifacts = []
        for row in enabled:
            playbook_id = str(row.get("playbook_id") or "")
            sha = _active_sha(row)
            review = reviews.get(playbook_id) or {}
            if not sha or str(review.get("artifact_sha256") or "") != sha:
                review = {}
            artifacts.append(
                {
                    **row,
                    "artifact_sha256": sha or None,
                    "source_sha256": row.get("source_digest"),
                    "activation_health": row.get("health"),
                    "reviewed_by": review.get("reviewed_by"),
                    "reviewed_at": review.get("reviewed_at"),
                    "v1_available": playbook_id in v1_ids,
                }
            )

        try:
            pending_events = await self.db.list_pending_events(limit=10_000)
        except Exception as exc:  # pragma: no cover - defensive live-daemon reporting
            logger.warning("cutover report: pending events unavailable", exc_info=True)
            evidence_errors.append(_unread_evidence("pending_events", exc))
            pending_events = []

        def _run_row(run):
            if isinstance(run, dict):
                return run
            return {
                name: getattr(run, name, None)
                for name in ("run_id", "playbook_id", "status", "started_at", "event_id")
            }

        active_v1_runs = []
        for status in ("running", "paused"):
            try:
                active_v1_runs.extend(
                    _run_row(run) for run in await self.db.list_playbook_runs(status=status, limit=10_000)
                )
            except Exception as exc:  # pragma: no cover - defensive live-daemon reporting
                logger.warning("cutover report: active V1 runs unavailable", exc_info=True)
                evidence_errors.append(
                    _unread_evidence("active_v1_runs", exc, detail=f"status={status}: ")
                )

        parity_path = fixture_root / "parity-report.json"
        try:
            import json

            parity = json.loads(parity_path.read_text(encoding="utf-8"))
            if not isinstance(parity, dict):
                raise ValueError("not an object")
            parity["recorded"] = True
        except Exception:
            parity = {
                "observations": 0,
                "identical": 0,
                "expected": 0,
                "unexplained": 0,
                "suite": "tests/test_playbook_shadow_parity.py",
                "recorded": False,
            }

        acknowledged_disabled = []
        try:
            acknowledgements = await self.db.list_playbook_migration_acks()
        except Exception as exc:  # pragma: no cover - optional reporting detail
            logger.warning("cutover report: acknowledgements unavailable", exc_info=True)
            evidence_errors.append(_unread_evidence("acknowledgements", exc))
            acknowledgements = []
        for entry in inventory.entries:
            if entry.disposition != "disabled" or entry.acknowledged_by is None:
                continue
            ack = next(
                (
                    row
                    for row in acknowledgements
                    if row.get("playbook_id") == entry.playbook_id
                    and row.get("source_sha256") == entry.source.source_sha256
                ),
                {},
            )
            acknowledged_disabled.append(
                {
                    "playbook_id": entry.playbook_id,
                    "reason": ack.get("reason"),
                    "acknowledged_by": entry.acknowledged_by,
                    "acknowledged_at": entry.acknowledged_at,
                }
            )
        return {
            "contract_fingerprint": str(CONTRACTS.registry_fingerprint()),
            "artifacts": artifacts,
            "unresolved": [entry.to_dict() for entry in inventory.blocking()],
            "acknowledged_disabled": acknowledged_disabled,
            "pending_events": pending_events,
            "active_v1_runs": active_v1_runs,
            "parity": parity,
            "evidence_errors": evidence_errors,
        }

    async def _cmd_playbook_cutover_report(self, args: dict) -> dict:
        """Report all evidence and blockers needed for a signed V2 cutover."""
        inputs = await self._cutover_report_inputs()
        return build_cutover_report(**inputs)
