"""Explicit selection orchestration; commands own authorization and events.

Only validated Jev answers are cached. Every invocation still gets its own
immutable record, and promotion is checked afresh even on a cache hit. Offline
planning uses the same pipeline without a database or transport.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from src.config import TestSelectionConfig
from src.git.manager import GitError, GitManager
from src.test_selection import reasons as r
from src.test_selection.catalogue import (
    CATALOGUE_PATH,
    POLICY_PATH,
    RULES_PATH,
    Catalogue,
    CatalogueError,
    load_catalogue,
    load_catalogue_text,
    load_policy,
    load_rules,
    validate_catalogue,
)
from src.test_selection.mandatory import mandatory_set
from src.test_selection.policy import Mode, PromotionIdentity, compose, order_modules
from src.test_selection.questions import (
    QUESTION_SCHEMA_VERSION,
    build_questions,
    build_state,
    pack_requests,
)
from src.test_selection.snapshot import ChangeSnapshot, snapshot_fingerprint, take_snapshot
from src.test_selection.static_impact import (
    PytestImpactedAdapter,
    StaticImpact,
    StaticResult,
    UnavailableStaticImpact,
)
from src.test_selection.typesafe import JevAdapter, JevResult, TypeSafeTransport


@dataclass(frozen=True)
class SelectionRequest:
    project_id: str
    task_id: str | None
    session_id: str | None
    claim_epoch: int | None
    workspace: str
    mode: Mode
    base_ref: str | None
    targets: tuple[str, ...]
    jev: bool
    marker_policy: str = "default"
    acceptance_commands: tuple[str, ...] = ()
    changed_dashboard: bool | None = None


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def cache_key(
    *,
    snapshot_fingerprint: str,
    catalogue_digest: str,
    rules_digest: str,
    policy_digest: str,
    model: str,
    question_schema_version: int,
    marker_policy: str,
    static_engine: str,
    project_id: str,
    evidence_digest: str = "",
) -> str:
    """Bind recommendations to a project, snapshot, artifacts and model inputs.

    The service also hashes the sanitized state: changing excerpt limits or
    computed static facts must not reuse an answer to different evidence.
    """
    return _digest(
        {
            "snapshot_fingerprint": snapshot_fingerprint,
            "catalogue_digest": catalogue_digest,
            "rules_digest": rules_digest,
            "policy_digest": policy_digest,
            "model": model,
            "question_schema_version": question_schema_version,
            "marker_policy": marker_policy,
            "static_engine": static_engine,
            "project_id": project_id,
            "evidence_digest": evidence_digest,
        }
    )


def pending_obligations(
    *,
    snapshot: ChangeSnapshot,
    catalogue: Catalogue | None,
    request: SelectionRequest,
    full_required: bool,
) -> list[dict]:
    obligations = [
        {
            "kind": "marker_arm",
            "command": "aq test -m 'migration or slow' tests/",
            "reason": "separate arm",
        },
        {
            "kind": "marker_arm",
            "command": "aq test -m 'integration or perf' tests/",
            "reason": "separate arm",
        },
    ]
    dashboard = request.changed_dashboard
    if dashboard is None:
        dashboard = any(path.startswith("dashboard/") for path in snapshot.paths)
    if dashboard:
        obligations.append({"kind": "frontend", "command": "npm --prefix dashboard test"})
    obligations.extend({"kind": "acceptance", "command": c} for c in request.acceptance_commands)
    if full_required:
        obligations.append({"kind": "full_suite", "command": "aq test tests/"})
    universe = catalogue.universe if catalogue is not None else frozenset()
    obligations.extend(
        {"kind": "explicit_target", "target": t}
        for t in dict.fromkeys(request.targets)
        if t not in universe
    )
    return obligations


def _load_artifacts(workspace: str):
    root = Path(workspace)
    catalogue = load_catalogue(root / CATALOGUE_PATH)
    problems = validate_catalogue(root, catalogue)
    if problems:
        raise CatalogueError(problems)
    return (
        catalogue,
        load_rules(root / RULES_PATH, catalogue=catalogue),
        load_policy(root / POLICY_PATH),
    )


class SelectionService:
    def __init__(
        self,
        *,
        db,
        git: GitManager,
        config_getter: Callable[[], TestSelectionConfig],
        static: StaticImpact,
        transport_factory: Callable[[TestSelectionConfig], TypeSafeTransport | None],
        default_branch_getter: Callable[[str], Awaitable[str]],
        clock=time.time,
    ) -> None:
        self.db = db
        self.git = git
        self._config_getter = config_getter
        self._static = static
        self._transport_factory = transport_factory
        self._default_branch_getter = default_branch_getter
        self._clock = clock
        self._cache: OrderedDict[str, JevResult] = OrderedDict()

    async def select(self, request: SelectionRequest, *, persist: bool = True) -> dict:
        config = self._config_getter()
        if not config.enabled:
            raise ValueError("disabled")
        if request.mode not in ("plan_only", "shadow", "enforce"):
            raise ValueError("invalid selection mode")
        if request.mode == "enforce" and not config.enforce_enabled:
            raise ValueError("enforce_disabled")
        if request.marker_policy not in ("default", "all"):
            raise ValueError("invalid marker policy")
        if persist and self.db is None:
            raise ValueError("selection persistence requires a database")
        while len(self._cache) > config.cache_entries:
            self._cache.popitem(last=False)
        elapsed: dict[str, float] = {}

        @contextmanager
        def stage(name):
            started = self._clock()
            try:
                yield
            finally:
                elapsed[name] = max(0.0, (self._clock() - started) * 1000)

        catalogue = rules = policy = None
        with stage("artifacts"):
            try:
                catalogue, rules, policy = await asyncio.to_thread(
                    _load_artifacts, request.workspace
                )
            except CatalogueError:
                # No trustworthy universe exists. Recommend tests/ explicitly;
                # never infer an empty selection from an unreadable catalogue.
                pass
        with stage("snapshot"):
            base_ref = request.base_ref or config.default_base_ref
            if not base_ref:
                base_ref = f"origin/{await self._default_branch_getter(request.project_id)}"
            # One extra line lets build_state detect excerpt truncation. The
            # snapshot fingerprint depends on contents, not excerpt length.
            snapshot = await take_snapshot(
                self.git,
                request.workspace,
                base_ref=base_ref,
                excerpt_lines=config.excerpt_lines + 1,
            )
        with stage("mandatory"):
            base_catalogue = None
            base_unusable = False
            if catalogue is not None and snapshot.complete and any(
                c.status in ("deleted", "renamed") for c in snapshot.changes
            ):
                # The workspace catalogue cannot list removed paths. Use the
                # merge-base blob for their ownership, without validating it
                # against the current tree or resolving the moving base ref.
                source = f"{snapshot.base_sha}:{CATALOGUE_PATH}"
                try:
                    blob = await self.git.arun_git_result(["show", source], cwd=snapshot.workspace)
                    if blob.returncode != 0:
                        raise CatalogueError([f"{source}: unreadable base catalogue"])
                    base_catalogue = load_catalogue_text(blob.stdout, source=source)
                except (GitError, CatalogueError):
                    base_unusable = True
            mandatory = (
                mandatory_set(snapshot, catalogue, rules, base_catalogue=base_catalogue)
                if catalogue is not None
                else None
            )
            if base_unusable:
                # Missing old ownership cannot justify narrowing. The current
                # catalogue still provides a trustworthy runnable universe.
                mandatory = replace(
                    mandatory,
                    modules=catalogue.universe,
                    full_required=True,
                    reasons={
                        m: (*mandatory.reasons.get(m, ()), r.CATALOGUE_UNUSABLE)
                        for m in sorted(catalogue.universe)
                    },
                    global_reasons=(r.CATALOGUE_UNUSABLE, *mandatory.global_reasons),
                )
        with stage("static"):
            if mandatory is None or mandatory.full_required:
                static = StaticResult(frozenset(), True, "skipped", None, 0, 0)
            else:
                try:
                    static = await asyncio.wait_for(
                        self._static.impacted(snapshot, catalogue=catalogue),
                        timeout=config.static_timeout_seconds,
                    )
                except TimeoutError:
                    static = StaticResult(frozenset(), False, "unavailable", "static_timeout", 0, 0)
        full = mandatory is None or mandatory.full_required or not static.complete
        with stage("packing"):
            state = packing = None
            questions = []
            if not full:
                state = build_state(
                    snapshot,
                    mandatory=mandatory,
                    static=static,
                    catalogue=catalogue,
                    excerpt_lines=config.excerpt_lines,
                )
                if any(c.new_blob == "oversize" or not c.excerpt for c in snapshot.changes):
                    # Binary, oversized or content-free diffs have a complete
                    # path inventory, but no complete model-visible evidence.
                    state = replace(state, evidence_complete=False)
                questions = build_questions(catalogue)
                packing = pack_requests(
                    state,
                    questions,
                    max_total_tokens=config.max_total_tokens,
                    max_state_plus_question_tokens=config.max_state_plus_question_tokens,
                    max_requests=config.max_requests,
                )
            key = cache_key(
                snapshot_fingerprint=snapshot.fingerprint(),
                catalogue_digest=catalogue.digest if catalogue is not None else "unusable",
                rules_digest=rules.digest if rules is not None else "unusable",
                policy_digest=policy.digest if policy is not None else "unusable",
                model=config.model,
                question_schema_version=QUESTION_SCHEMA_VERSION,
                marker_policy=request.marker_policy,
                static_engine=static.engine,
                project_id=request.project_id,
                evidence_digest=_digest(state.to_json()) if state is not None else "",
            )
        with stage("jev"):
            jev = None
            status, jev_reason, cached = "disabled", r.JEV_DISABLED, False
            if not full and request.jev and config.jev_enabled:
                transport = self._transport_factory(config)
                if transport is None:
                    status, jev_reason = "unconfigured", r.JEV_UNCONFIGURED
                elif packing.complete and key in self._cache:
                    jev = self._cache[key]
                    self._cache.move_to_end(key)
                    cached = True
                else:
                    jev = await JevAdapter(
                        transport,
                        model=config.model,
                        deadline_seconds=config.rpc_deadline_seconds,
                        concurrency=config.request_concurrency,
                    ).evaluate(packing, area_by_key={q.key: q.area_id for q in questions})
                    if jev.complete:
                        self._cache[key] = jev
                        while len(self._cache) > config.cache_entries:
                            self._cache.popitem(last=False)
                if jev is not None:
                    status, jev_reason = jev.status, jev.reason
        with stage("promotion"):
            promotion = None
            promoted = False
            if catalogue is not None and self.db is not None:
                promotion = await self.db.active_test_selection_promotion(
                    project_id=request.project_id
                )
                identity = PromotionIdentity(
                    jev.returned_model if jev is not None and jev.returned_model else config.model,
                    QUESTION_SCHEMA_VERSION,
                    catalogue.digest,
                    rules.digest,
                    policy.digest,
                )
                if promotion is not None:
                    recorded = PromotionIdentity(
                        **{
                            field: promotion[field]
                            for field in PromotionIdentity.__dataclass_fields__
                        }
                    )
                    promoted = recorded.matches(identity) and (
                        identity.model == policy.model
                        and identity.question_schema_version == policy.question_schema_version
                    )
        with stage("compose"):
            composition = None
            if catalogue is not None:
                # Preserve the disabled/unconfigured reason without treating
                # the absence of answers as successful evidence.
                unavailable = JevResult(status, {}, config.model, None, jev_reason, 0, 0, 0, 0)
                composition = compose(
                    catalogue=catalogue,
                    mandatory=mandatory,
                    static=static,
                    jev=jev or unavailable,
                    policy=policy,
                    mode=request.mode,
                    promoted=promoted,
                    evidence_complete=state.evidence_complete if state is not None else False,
                )
                final = composition.final | (set(request.targets) & catalogue.universe)
                module_reasons = {m: list(codes) for m, codes in composition.reasons.items()}
                for target in request.targets:
                    if target in catalogue.universe:
                        codes = module_reasons.setdefault(target, [])
                        if r.EXPLICIT_TARGET not in codes:
                            codes.append(r.EXPLICIT_TARGET)
                ordered = order_modules(
                    final,
                    area_decisions=composition.area_decisions,
                    catalogue=catalogue,
                    durations=None,
                )
            else:
                final, module_reasons, ordered = frozenset(), {}, ("tests/",)
        with stage("argv"):
            # Node IDs remain pending obligations, never module-plan argv.
            extra = [t for t in request.targets if "::" not in t and t not in ordered]
            argv = [list(dict.fromkeys((*ordered, *extra)))]
            obligations = pending_obligations(
                snapshot=snapshot, catalogue=catalogue, request=request, full_required=full
            )
        values = {
            "project_id": request.project_id,
            "task_id": request.task_id,
            "session_id": request.session_id,
            "claim_epoch": request.claim_epoch,
            "mode": request.mode,
            "workspace": snapshot.workspace,
            "base_ref": snapshot.base_ref,
            "base_sha": snapshot.base_sha,
            "head_sha": snapshot.head_sha,
            "dirty_fingerprint": snapshot.dirty_fingerprint,
            "snapshot_fingerprint": snapshot.fingerprint(),
            "snapshot_complete": snapshot.complete,
            "incomplete_reason": snapshot.incomplete_reason,
            "catalogue_digest": catalogue.digest if catalogue is not None else "unusable",
            "rules_digest": rules.digest if rules is not None else "unusable",
            "policy_digest": policy.digest if policy is not None else "unusable",
            "question_schema_version": QUESTION_SCHEMA_VERSION,
            "static_engine": static.engine,
            "marker_policy": request.marker_policy,
            "cache_key": key,
            "jev_requested_model": config.model,
            "jev_returned_model": jev.returned_model if jev is not None else None,
            "jev_status": status,
            "fallback_reason": composition.fallback_reason if composition else r.CATALOGUE_UNUSABLE,
            "full_required": full,
            "jev_used_for_omission": composition.jev_used_for_omission if composition else False,
            "promotion_id": promotion["id"] if promoted else None,
            "area_decisions": {a: asdict(d) for a, d in composition.area_decisions.items()}
            if composition
            else {},
            "mandatory_modules": sorted(composition.mandatory) if composition else [],
            "static_modules": sorted(composition.static) if composition else [],
            "jev_modules": sorted(composition.jev)
            if composition and composition.jev is not None
            else None,
            "fallback_modules": sorted(composition.fallback) if composition else [],
            "final_modules": sorted(final),
            "reasons": module_reasons,
            "pending_obligations": obligations,
            "argv": argv,
            "elapsed_ms": elapsed,
            "usage": {
                "input_tokens": jev.input_tokens if jev is not None else 0,
                "output_tokens": jev.output_tokens if jev is not None else 0,
                "requests": 0 if cached or jev is None else jev.requests,
                "reasons": [r.JEV_CACHED] if cached else [],
                "evidence_complete": state.evidence_complete if state is not None else False,
            },
            "created_at": self._clock(),
        }
        if persist:
            values = await self.db.insert_test_selection(values)
        return {**values, "recorded": persist}

    async def recheck(self, selection_id: str) -> dict:
        record = await self.db.get_test_selection(selection_id)
        if record is None:
            raise LookupError(f"unknown test selection: {selection_id}")
        fingerprint = await snapshot_fingerprint(
            self.git, record["workspace"], base_ref=record["base_ref"]
        )
        return {
            "stale": fingerprint != record["snapshot_fingerprint"],
            "fingerprint": fingerprint,
            "recorded_fingerprint": record["snapshot_fingerprint"],
        }

    async def observe(
        self,
        selection_id: str,
        *,
        kind: str,
        source: str,
        exit_code: int | None,
        duration_ms: int | None,
        executed_modules: Sequence[str],
        failed_node_ids: Sequence[str],
        payload: dict,
    ) -> dict:
        return await self.db.append_test_selection_observation(
            selection_id=selection_id,
            kind=kind,
            source=source,
            exit_code=exit_code,
            duration_ms=duration_ms,
            executed_modules=list(executed_modules),
            failed_node_ids=list(failed_node_ids),
            payload=payload,
            observed_at=self._clock(),
        )


def select_offline(
    workspace: str,
    *,
    base_ref: str,
    targets: Sequence[str],
    marker_policy: str = "default",
) -> dict:
    """Return an unrecorded, network-free plan when the daemon is unavailable."""

    async def default_branch(project_id: str) -> str:
        return "main"  # base_ref is required, so this getter is never used

    static = (
        PytestImpactedAdapter() if shutil.which("impacted-tests") else UnavailableStaticImpact()
    )
    service = SelectionService(
        db=None,
        git=GitManager(),
        config_getter=lambda: TestSelectionConfig(enabled=True),
        static=static,
        transport_factory=lambda config: None,
        default_branch_getter=default_branch,
    )
    request = SelectionRequest(
        "offline",
        None,
        None,
        None,
        workspace,
        "plan_only",
        base_ref,
        tuple(targets),
        False,
        marker_policy,
    )
    return asyncio.run(service.select(request, persist=False))
