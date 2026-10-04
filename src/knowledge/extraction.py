"""Bounded paid generation into proposals, with retained results and honest ambiguity."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from dataclasses import dataclass, replace
from types import SimpleNamespace

from src.commands.principal import ExecutionPrincipal
from src.env_scrub import is_sensitive
from src.knowledge.capture import CAPTURE_VERSION, KnowledgeCapture, partition_inputs, CaptureInput
from src.knowledge.extraction_store import ExtractionStore, MAX_CALL_TOKENS
from src.knowledge.models import canonical_bytes, content_hash, normalize_snapshot
from src.knowledge.registration import provider_id_of, provider_version_of
from src.knowledge.service import KnowledgeService
from src.profiles.capabilities import CapabilityPolicy
from src.records.artifacts import RetainedArtifacts
from src.records.models import RecordError

logger = logging.getLogger(__name__)
EXTRACTION_SERVICE = "knowledge_extraction"
MAX_OUTPUT_BYTES = 262144
SENSITIVE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16})\b|"
    r"(?i:\b(?:password|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*\S+)|"
    r"(?i:authorization\s*:\s*bearer\s+\S+)|"
    r"\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s/]*:[^\s/]*@",
)
SOURCE_ASSIGNMENTS = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]*)[\"']?\s*[:=]\s*(\S+)")


def sensitive_source(content):
    return bool(SENSITIVE.search(content)) or any(
        is_sensitive(match[1], match[2].strip("\"',;"))
        for match in SOURCE_ASSIGNMENTS.finditer(content)
    )


def proposal_principal(project):
    return replace(
        ExecutionPrincipal.service("knowledge-extraction"),
        project_id=project,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["knowledge_propose"]),
    )


@dataclass(frozen=True)
class ExtractionRequest:
    operation_id: str
    feature: str
    scope_key: str
    inputs: tuple[dict, ...]
    max_input_tokens: int = MAX_CALL_TOKENS
    max_outputs: int = 8

    def wire(self):
        return dict(
            operation_id=self.operation_id,
            feature=self.feature,
            scope_key=self.scope_key,
            inputs=list(self.inputs),
            max_input_tokens=self.max_input_tokens,
            max_outputs=self.max_outputs,
        )


class ExtractionProviderRegistry:
    """Only look up an already loaded adapter; never initialize legacy extraction."""

    def __init__(self, get_service):
        self.get_service = get_service

    def lookup(self, provider_id):
        try:
            provider = self.get_service(EXTRACTION_SERVICE)
            if (
                provider is None
                or provider_id_of(provider.provider_id) != provider_id
                or provider_version_of(provider.provider_version) is None
                or not getattr(provider, "available", False)
                or getattr(provider, "deprecated", False)
                or not inspect.iscoroutinefunction(getattr(provider, "generate", None))
                or not callable(getattr(provider, "estimate", None))
                or inspect.iscoroutinefunction(provider.estimate)
            ):
                return None
            return provider
        except Exception:
            return None


class ExtractionWorker:
    def __init__(self, db, config, *, registry=None, store=None, timeout=15):
        self.db = db
        self._config = config
        self.store = store or ExtractionStore()
        self.registry = registry or ExtractionProviderRegistry(lambda name: None)
        self.timeout = min(timeout, 15)
        self._tick_lock = asyncio.Lock()
        self._scope_cursor = 0

    @property
    def config(self):
        return self._config() if callable(self._config) else self._config

    def artifacts(self):
        return RetainedArtifacts(self.db, self.config.knowledge, self.config.vault_root)

    def enabled(self, feature, project):
        return KnowledgeCapture(self.db, self._config).enabled(feature, project)

    async def _prepare_on(self, job, *, conn):
        """Hydrate retained bytes and recheck all source scopes/policies before I/O."""
        receipts = await self.store.inputs_on(job["job_id"], conn=conn)
        inputs, sources, envelopes = [], [], []
        for receipt in receipts:
            raw, artifact = await self.artifacts().read_on(
                receipt["artifact_id"], scope_key=job["scope_key"], conn=conn
            )
            try:
                envelope = json.loads(raw)
                envelopes.append(dict(envelope))
                feature = envelope.pop("feature")
                if envelope.pop("format_version") != 1:
                    raise ValueError()
                item = CaptureInput(**envelope)
            except (TypeError, ValueError, KeyError):
                raise RecordError("extraction.invalid_input") from None
            settings = getattr(self.config.knowledge, feature, None)
            if (
                feature not in {"extraction", "consolidation"}
                or settings is None
                or not self.enabled(feature, job["scope_key"].removeprefix("project:"))
            ):
                raise RecordError("knowledge.disabled")
            if (
                item.scope_key != job["scope_key"]
                or item.actor_id != receipt["actor_id"]
                or item.event_id != receipt["event_id"]
                or item.attempt_id != receipt["attempt_id"]
                or item.source_policy != job["policy_version"]
                or settings.policy_version != job["policy_version"]
                or job["extractor_version"] != f"{feature}:{CAPTURE_VERSION}"
            ):
                raise RecordError("extraction.source_policy_changed")
            if item.content is None:
                raise RecordError("extraction.source_unavailable")
            if sensitive_source(item.content):
                raise RecordError("extraction.sensitive_source")
            if (
                item.provider_id != settings.provider_id
                or item.provider_id not in settings.allowed_providers
            ):
                raise RecordError("extraction.provider_not_allowed")
            inputs.append(item)
            sources.append(
                dict(
                    source_id=str(artifact["artifact_id"]),
                    kind="artifact",
                    artifact_id=str(artifact["artifact_id"]),
                    sha256=artifact["content_sha256"],
                )
            )
        if content_hash(envelopes) != job["source_sha256"]:
            raise RecordError("extraction.source_unavailable")
        if len(partition_inputs(inputs)) != 1:
            raise RecordError("extraction.input_partition")
        project = job["scope_key"].removeprefix("project:")
        service = KnowledgeService(self.db, self.config.knowledge)
        principal = proposal_principal(project)
        access = await service._access(conn, principal, "knowledge_propose", project, write=True)
        for source in sources:
            await service._source_visible(source, access, conn=conn, writing=True)
        for item in inputs:
            if item.target_record_id:
                record = await service.resolve_on(
                    f"record:{item.target_record_id}", access, conn=conn
                )
                revision = await service._revision(record, item.target_revision_id, conn=conn)
                current_revision = await service._revision(record, conn=conn)
                if (
                    revision["revision_id"] != current_revision["revision_id"]
                    or revision["snapshot"]["lifecycle"] != "active"
                    or revision["snapshot"]["verification"] == "disputed"
                ):
                    raise RecordError("extraction.source_unavailable")
                await service._validate_snapshot_access(revision["snapshot"], access, conn=conn)
        return feature, inputs, sources

    async def _finish(self, job, state, code=None):
        async with self.db.immediate() as conn:
            return await self.store.finish_on(
                job["job_id"], job["lease_token"], state=state, error_code=code, conn=conn
            )

    async def _unknown(self, job, feature):
        async with self.db.immediate() as conn:
            await self.store.unknown_on(job["job_id"], job["lease_token"], conn=conn)
            await self.store.provider_result_on(
                scope_key=job["scope_key"], feature=feature, failed=True, conn=conn
            )

    async def _reject_result(self, job, feature, result):
        """Malformed content cannot erase a provider's valid usage report."""
        async with self.db.immediate() as conn:
            await self.store.settle_on(
                job["job_id"],
                job["lease_token"],
                actual_microusd=result["actual_microusd"],
                actual_tokens=result["actual_tokens"],
                conn=conn,
            )
            await self.store.finish_on(
                job["job_id"],
                job["lease_token"],
                state="quarantined",
                error_code="extraction.invalid_output",
                conn=conn,
            )
            await self.store.provider_result_on(
                scope_key=job["scope_key"], feature=feature, failed=True, conn=conn
            )

    async def _publish(self, job):
        """Saved output, exact-base proposals and job completion commit atomically."""
        async with self.db.immediate() as conn:
            current = await self.store._leased_on(job["job_id"], job["lease_token"], conn=conn)
            feature, inputs, sources = await self._prepare_on(current, conn=conn)
            raw, artifact = await self.artifacts().read_on(
                current["result_artifact_id"], scope_key=job["scope_key"], conn=conn
            )
            try:
                result = json.loads(raw)
                if set(result) != {
                    "outputs",
                    "actual_microusd",
                    "actual_tokens",
                    "provider_version",
                }:
                    raise ValueError()
                if not isinstance(result["outputs"], list) or len(result["outputs"]) > 8:
                    raise ValueError()
                if provider_version_of(result["provider_version"]) is None:
                    raise ValueError()
            except (ValueError, TypeError):
                raise RecordError("extraction.invalid_output") from None
            await self.store.settle_on(
                job["job_id"],
                job["lease_token"],
                actual_microusd=result["actual_microusd"],
                actual_tokens=result["actual_tokens"],
                conn=conn,
            )
            service = KnowledgeService(self.db, self.config.knowledge)
            project = job["scope_key"].removeprefix("project:")
            principal = proposal_principal(project)
            proposals = []
            for ordinal, output in enumerate(result["outputs"]):
                if (
                    not isinstance(output, dict)
                    or set(output) - {"title", "body", "category", "summary", "tags"}
                    or not {"title", "body", "category"} <= set(output)
                ):
                    raise RecordError("extraction.invalid_output")
                identity, base, links, original_sources = None, None, [], []
                if feature == "consolidation":
                    if len(inputs) != 1 or not inputs[0].target_record_id:
                        raise RecordError("extraction.invalid_input")
                    identity, base = (
                        f"record:{inputs[0].target_record_id}",
                        inputs[0].target_revision_id,
                    )
                    access = await service._access(
                        conn, principal, "knowledge_propose", project, write=True
                    )
                    record = await service.resolve_on(identity, access, conn=conn)
                    revision = await service._revision(record, base, conn=conn)
                    links, original_sources = (
                        revision["snapshot"]["outgoing_links"],
                        revision["snapshot"]["sources"],
                    )
                all_sources = {
                    source["source_id"]: source
                    for source in [
                        *original_sources,
                        *sources,
                        dict(
                            source_id=str(artifact["artifact_id"]),
                            kind="artifact",
                            artifact_id=str(artifact["artifact_id"]),
                            sha256=artifact["content_sha256"],
                        ),
                    ]
                }
                try:
                    snapshot = normalize_snapshot(
                        {
                            **output,
                            "sources": list(all_sources.values()),
                            "outgoing_links": links,
                            "verification": "unverified",
                            "lifecycle": "active",
                            "metadata": {
                                "extraction.job_id": str(job["job_id"]),
                                "extraction.feature": feature,
                                "extraction.evidence_types": [
                                    item.evidence_type for item in inputs
                                ],
                            },
                        }
                    )
                except RecordError as exc:
                    raise RecordError("extraction.invalid_output") from exc
                proposals.append(
                    await service.propose_on(
                        snapshot=snapshot,
                        principal=principal,
                        project_id=project,
                        identity=identity,
                        if_revision=base,
                        idempotency_key=f"extraction:{job['job_id']}:{ordinal}",
                        conn=conn,
                    )
                )
            await self.store.provider_result_on(
                scope_key=job["scope_key"], feature=feature, failed=False, conn=conn
            )
            await self.store.finish_on(
                job["job_id"], job["lease_token"], state="succeeded", conn=conn
            )
            return proposals

    async def run_one(self, job):
        feature = job["extractor_version"].split(":", 1)[0]
        project = job["scope_key"].removeprefix("project:")
        if not self.enabled(feature, project):
            return await self._finish(job, "retry", "disabled")
        try:
            # A saved result bypasses provider lookup, estimation and paid generation.
            if job["result_artifact_id"]:
                await self._publish(job)
                return "succeeded"
            if not getattr(self.config.knowledge, feature).provider_id:
                return await self._finish(job, "retry", "provider_unconfigured")
            async with self.db.immediate() as conn:
                feature, inputs, _ = await self._prepare_on(job, conn=conn)
            settings = getattr(self.config.knowledge, feature)
            provider = self.registry.lookup(settings.provider_id)
            if provider is None:
                return await self._finish(job, "retry", "provider_unavailable")
            request = ExtractionRequest(
                operation_id=f"knowledge:{job['job_id']}",
                feature=feature,
                scope_key=job["scope_key"],
                inputs=tuple(
                    {
                        "ordinal": ordinal,
                        "content": item.content,
                        "evidence_type": item.evidence_type,
                        "source_identity": item.source_identity,
                        "event_id": item.event_id,
                        "attempt_id": item.attempt_id,
                        "line_start": item.line_start,
                        "line_end": item.line_end,
                    }
                    for ordinal, item in enumerate(inputs)
                ),
            )
            token_bound = len(canonical_bytes(request.wire()))
            if token_bound > MAX_CALL_TOKENS:
                raise RecordError("extraction.call_token_limit")
            estimate = provider.estimate(request)
            if (
                not isinstance(estimate, dict)
                or set(estimate) != {"microusd", "tokens"}
                or type(estimate["tokens"]) is not int
                or estimate["tokens"] < token_bound
            ):
                raise RecordError("extraction.invalid_estimate")
            async with self.db.immediate() as conn:
                # Recheck live config and the retained sources immediately before admission.
                await self._prepare_on(job, conn=conn)
                settings = getattr(self.config.knowledge, feature)
                await self.store.configure_budget_on(
                    scope_key=job["scope_key"],
                    feature=feature,
                    limit_microusd=settings.daily_microusd,
                    token_limit=settings.daily_tokens,
                    conn=conn,
                )
                await self.store.reserve_on(
                    job["job_id"],
                    job["lease_token"],
                    feature=feature,
                    estimated_microusd=estimate["microusd"],
                    estimated_tokens=estimate["tokens"],
                    conn=conn,
                )
                start = await self.store.begin_operation_on(
                    job["job_id"],
                    job["lease_token"],
                    provider_operation_id=request.operation_id,
                    conn=conn,
                )
            if not start:
                await self._unknown(job, feature)
                return "quarantined"
            try:
                async with asyncio.timeout(self.timeout):
                    result = await provider.generate(request)
            except asyncio.CancelledError:
                # Cancellation may happen after a provider charged the call. The
                # persisted operation marker also fences restart after hard crash.
                await asyncio.shield(self._unknown(job, feature))
                raise
            except Exception:
                await self._unknown(job, feature)
                return "quarantined"
            # Validate actual usage before retaining. Invalid or absent usage is
            # an unknown charge, never a zero-cost successful call.
            if not isinstance(result, dict) or any(
                type(result.get(key)) is not int or not 0 <= result[key] <= 2**63 - 1
                for key in ("actual_microusd", "actual_tokens")
            ):
                await self._unknown(job, feature)
                return "quarantined"
            saved = {**result, "provider_version": provider.provider_version}
            try:
                raw = canonical_bytes(saved)
            except (ValueError, TypeError):
                await self._reject_result(job, feature, saved)
                return "quarantined"
            if len(raw) > MAX_OUTPUT_BYTES:
                await self._reject_result(job, feature, saved)
                return "quarantined"
            async with self.db.immediate() as conn:
                source = await self.artifacts().retain_on(
                    raw, access=SimpleNamespace(scope_key=job["scope_key"]), conn=conn
                )
                await self.store.save_output_on(
                    job["job_id"], job["lease_token"], artifact_id=source["artifact_id"], conn=conn
                )
            # Settle separately so rejected proposal content cannot roll back the charge.
            async with self.db.immediate() as conn:
                await self.store.settle_on(
                    job["job_id"],
                    job["lease_token"],
                    actual_microusd=saved["actual_microusd"],
                    actual_tokens=saved["actual_tokens"],
                    conn=conn,
                )
            await self._publish(job)
            return "succeeded"
        except RecordError as exc:
            code = exc.code
            if code == "extraction.stale_lease":
                return "stale_lease"
            retry = code in {
                "knowledge.disabled",
                "knowledge.read_only",
                "record.retryable",
                "extraction.budget_disabled",
                "extraction.budget_exceeded",
                "extraction.budget_circuit_open",
            }
            state = await self._finish(job, "retry" if retry else "quarantined", code)
            if code == "extraction.invalid_output":
                async with self.db.immediate() as conn:
                    await self.store.provider_result_on(
                        scope_key=job["scope_key"], feature=feature, failed=True, conn=conn
                    )
            return state
        except Exception:
            # The paid-call exception path has already quarantined the operation.
            # A crash in later persistence leaves the lease/output for recovery.
            logger.warning("Knowledge generation interrupted; durable state retained")
            return "interrupted"

    async def tick(self):
        cfg = self.config
        scopes = [
            f"project:{p}"
            for p in cfg.knowledge.enabled_projects
            if any(self.enabled(feature, p) for feature in ("extraction", "consolidation"))
        ]
        if not scopes or self._tick_lock.locked():
            return []
        async with self._tick_lock:
            offset = self._scope_cursor % len(scopes)
            scopes = scopes[offset:] + scopes[:offset]
            self._scope_cursor += 2
            async with self.db.immediate() as conn:
                versions = {
                    scope: [
                        f"{feature}:{CAPTURE_VERSION}"
                        for feature in ("extraction", "consolidation")
                        if self.enabled(feature, scope.removeprefix("project:"))
                    ]
                    for scope in scopes
                }
                jobs = await self.store.claim_due_on(
                    scope_keys=scopes, conn=conn, versions_by_scope=versions
                )
            return await asyncio.gather(*(self.run_one(job) for job in jobs))


class KnowledgeGenerationLoop:
    """Wake-only bus subscriptions and a bounded command dispatch lifecycle."""

    def __init__(self, commands, config, bus, *, interval=5):
        self.commands, self.config, self.bus, self.interval = commands, config, bus, interval
        self._wake = asyncio.Event()
        self._task = None
        self._unsubscribers = []

    def start(self):
        if self._task is None:
            self._unsubscribers = [
                self.bus.subscribe(name, self.wake)
                for name in ("task.completed", "task_completed", "record.updated")
            ]
            self._task = asyncio.create_task(self._run())

    def wake(self, _):
        self._wake.set()

    async def _run(self):
        from src.commands.principal import principal_context

        while True:
            try:
                cfg = self.config()
                if (
                    cfg.knowledge.enabled
                    and cfg.memory.enabled
                    and any(
                        getattr(cfg.knowledge, feature).enabled
                        for feature in ("extraction", "consolidation")
                    )
                ):
                    handler = self.commands()
                    if handler is not None:
                        with principal_context(ExecutionPrincipal.service("knowledge-generation")):
                            await handler.execute("knowledge_generation_tick", {})
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Knowledge generation loop failed; retained inputs will reconcile")
            try:
                await asyncio.wait_for(self._wake.wait(), self.interval)
            except TimeoutError:
                pass
            self._wake.clear()

    async def stop(self):
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers = []
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
