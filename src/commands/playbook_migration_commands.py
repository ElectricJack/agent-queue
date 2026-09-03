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
from src.playbooks.migration import build_cutover_report, build_inventory, release_check

logger = logging.getLogger(__name__)

#: Exact operator-facing error text; the CLI and the dashboard match on it.
REASON_TOO_SHORT_ERROR = (
    f"an acknowledgement reason must be at least {MIN_ACK_REASON_LENGTH} characters — "
    "an empty waiver is not a waiver"
)


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

    async def _release_check_activations(self) -> list[dict]:
        """Enabled activations, each with the artifact's per-command fingerprints.

        An activation whose artifact cannot be loaded is **skipped, not failed**:
        its condition is already `unavailable` in the activation health surface,
        and reporting it a second time here as contract drift would name the
        wrong cause.

        Each row also carries `current_profiles`, resolved from this daemon's
        *live* profile registry rather than from the shipped defaults.  An
        activated artifact was compiled against the profiles in this database,
        operator edits included, so holding it to `src/profiles/defaults/`
        would report a legitimately customised profile as drift.

        `acknowledged_by` comes from the waiver table: `release_check` skips an
        acknowledged playbook because an operator has already decided about it,
        and activation rows carry no such column of their own.
        """
        rows: list[dict] = []
        try:
            activations = await self.db.list_playbook_activations()
        except Exception:  # pragma: no cover - defensive
            logger.warning("release check: activation rows unavailable", exc_info=True)
            return rows
        try:
            acknowledged = {
                (
                    str(ack.get("playbook_id") or ""),
                    str(ack.get("scope") or ""),
                    str(ack.get("scope_identifier") or ""),
                ): ack.get("acknowledged_by")
                for ack in await self.db.list_playbook_migration_acks()
            }
        except Exception:  # pragma: no cover - defensive
            logger.warning("release check: acknowledgements unavailable", exc_info=True)
            acknowledged = {}
        try:
            from src.playbooks.artifact_store import ArtifactStore

            store = ArtifactStore(
                self.config.compiled_root,
                max_artifact_bytes=self.config.playbooks.v2_max_artifact_bytes,
            )
        except Exception:  # pragma: no cover - defensive
            logger.debug("release check: artifact store unavailable", exc_info=True)
            return rows
        try:
            _contracts, profile_lookup, _events = await self._v2_lookups()
        except Exception:  # pragma: no cover - defensive
            logger.warning("release check: profile registry unavailable", exc_info=True)
            profile_lookup = None
        for row in activations:
            if not isinstance(row, dict) or not row.get("enabled", True):
                continue
            # `playbook_activations` names the column `active_artifact_sha256`;
            # reading `artifact_sha256` here silently skipped every row, which
            # is why no activation had ever reached the gate.
            sha = str(row.get("active_artifact_sha256") or "")
            if not sha:
                continue
            try:
                definition = store.load(sha)
            except Exception:
                logger.debug("release check: artifact %s unreadable", sha, exc_info=True)
                continue
            playbook_id = str(row.get("playbook_id") or "")
            artifact_profiles = dict(definition.compiled_against.profiles)
            entry = {
                "playbook_id": playbook_id,
                "enabled": True,
                "acknowledged_by": acknowledged.get(
                    (
                        playbook_id,
                        str(row.get("scope") or ""),
                        str(row.get("scope_identifier") or ""),
                    )
                ),
                "artifact_commands": dict(definition.compiled_against.commands),
                "artifact_profiles": artifact_profiles,
            }
            if profile_lookup is not None:
                from src.playbooks.migration import profile_fingerprints_for

                entry["current_profiles"] = profile_fingerprints_for(
                    profile_lookup, artifact_profiles
                )
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
        """
        from src.commands.contracts import CONTRACTS

        try:
            return release_check(
                contract_registry=CONTRACTS,
                activations=await self._release_check_activations(),
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("release check failed", exc_info=True)
            return {"success": False, "stale": [], "checked": [], "error": str(exc)}

    async def _cutover_report_inputs(self) -> dict:
        """Collect report inputs without turning the report into an operation.

        Missing optional read surfaces are rendered as unavailable evidence;
        the report remains honest and blocks cutover rather than failing open.
        """
        from src.commands.contracts import CONTRACTS
        from src.playbooks.migration import REVIEWED_FIXTURE_ROOT

        inventory = await self._migration_inventory()
        try:
            activation_rows = await self.db.list_playbook_activations()
        except Exception:  # pragma: no cover - defensive live-daemon reporting
            logger.warning("cutover report: activation rows unavailable", exc_info=True)
            activation_rows = []
        enabled = [row for row in activation_rows if isinstance(row, dict) and row.get("enabled", True)]

        store = self._migration_store()
        try:
            v1_ids = {
                str(getattr(playbook, "id", "") or "")
                for _scope, _identifier, playbook in (store.list_all() if store is not None else [])
            }
        except Exception:  # pragma: no cover - read-only fallback
            logger.warning("cutover report: V1 store unavailable", exc_info=True)
            v1_ids = set()
        artifacts = [
            {
                **row,
                "source_sha256": row.get("source_digest"),
                "activation_health": row.get("health"),
                "v1_available": str(row.get("playbook_id") or "") in v1_ids,
            }
            for row in enabled
        ]

        try:
            pending_events = await self.db.list_pending_events(limit=10_000)
        except Exception:  # pragma: no cover - defensive live-daemon reporting
            logger.warning("cutover report: pending events unavailable", exc_info=True)
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
            except Exception:  # pragma: no cover - defensive live-daemon reporting
                logger.warning("cutover report: active V1 runs unavailable", exc_info=True)

        parity_path = Path(REVIEWED_FIXTURE_ROOT) / "parity-report.json"
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
        except Exception:  # pragma: no cover - optional reporting detail
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
        }

    async def _cmd_playbook_cutover_report(self, args: dict) -> dict:
        """Report all evidence and blockers needed for a signed V2 cutover."""
        inputs = await self._cutover_report_inputs()
        return build_cutover_report(**inputs)
