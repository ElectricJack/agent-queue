"""Offline lexical/no-memory/semantic comparison ahead of gate G5 (K12).

This measures the *harness*, not a model. Every arm runs against the same
authorized corpus, queries and budget through the ordinary core paths: the
lexical arm is ``KnowledgeService.search``, the semantic arm is
``KnowledgeRetrieval`` over a deterministic offline provider whose chunks come
from the real hydration and receipt path, and the no-memory arm injects nothing.

No embedding, Milvus, network or model call happens here, so the result is a
pre-gate reading of the plumbing plus the declared metrics
(approved plan section 13). It cannot satisfy G5: that gate needs a measured
benefit on at least one declared retrieval metric from a real provider, with
repeated runs and an operator's approval of scope and cost. The exact missing
evidence is reported as :data:`G5_MISSING_EVIDENCE` instead of being implied.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from src.config import MemoryConfig
from src.knowledge.index_receipts import DerivedIndexReceipts
from src.knowledge.providers import KnowledgeRetrieval, RetrievalReference, RetrievalRequest
from src.knowledge.registration import RetrievalProviderRegistry

TOP_K = 8

G5_MISSING_EVIDENCE = (
    "a real installed provider (embeddings and its own managed index), not a deterministic fake",
    "at least three runs per arm per scenario, with recorded variance and raw receipts",
    "a blinded reviewer-approved measured benefit on at least one declared retrieval metric",
    "an explicit operator decision selecting the provider, scope and cost budget",
    "protected-scenario evidence from an authorized retained incident artifact, not only synthetic fixtures",
)

#: Metrics the plan declares for the offline comparison.
DECLARED_METRICS = (
    "precision_at_8",
    "required_evidence_recall",
    "exact_citation_rate",
    "ineligible_selection_rate",
    "empty_bundle_accuracy",
    "duplicate_rate",
    "added_input_tokens_upper_bound",
    "latency_ms",
    "lexical_fallbacks",
)

ARMS = ("no_memory", "lexical", "semantic")


@dataclass(frozen=True)
class Scenario:
    """One query over the shared corpus, with its independent oracle."""

    scenario_id: str
    query: str
    required: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    expects_empty: bool = False
    protected: bool = False


@dataclass
class Corpus:
    """Label to exact revision, plus the ineligible labels an arm must never serve."""

    revisions: dict[str, dict] = field(default_factory=dict)
    ineligible: set[str] = field(default_factory=set)
    labels: tuple[str, ...] = ()


SCENARIOS = (
    Scenario(
        scenario_id="current_plus_superseded",
        query="deploy staging rail health gate",
        required=("current_procedure",),
        forbidden=("superseded_procedure",),
        protected=True,
    ),
    Scenario(
        scenario_id="conflicting_claims",
        query="azure vault 92 incident cause",
        required=("incident_evidence", "prior_summary"),
        protected=True,
    ),
    Scenario(
        scenario_id="cross_project_secret",
        query="cross project confidential rotation key",
        forbidden=("private_record",),
        protected=True,
    ),
    Scenario(
        scenario_id="irrelevant_near_match",
        query="quarterly revenue forecast spreadsheet",
        expects_empty=True,
        protected=True,
    ),
    Scenario(
        scenario_id="retired_fact",
        query="milvus collection retention days",
        required=("current_fact",),
        forbidden=("retired_fact",),
        protected=True,
    ),
    Scenario(
        scenario_id="disputed_claim",
        query="provider failover ordering policy",
        required=("current_claim",),
        forbidden=("disputed_claim",),
        protected=True,
    ),
)


class OfflineTermIndex:
    """A deterministic in-process index: term overlap over acknowledged chunks.

    It holds only what core handed it through the hydration and receipt path, so
    a record it cannot serve has no acknowledged chunk at all. No embedding,
    vector store, network or filesystem is involved.
    """

    provider_id = "offline-terms"
    provider_version = "offline-v1"
    deprecated = False

    def __init__(self):
        self.available = True
        self.chunks: dict[tuple[str, str, str], str] = {}
        self.indexer = self

    # -- derived index half ----------------------------------------------

    async def index(self, payload: dict) -> dict:
        """Chunk one hydrated revision into a contiguous, text-free manifest."""

        body = payload["body"]
        split = max(1, len(body) // 2)
        spans = ((0, split), (split, len(body)))
        chunks = [
            {
                "char_end": end,
                "char_start": start,
                "chunk_id": f"chunk-{ordinal}",
                "ordinal": ordinal,
            }
            for ordinal, (start, end) in enumerate(spans)
            if end > start
        ]
        for chunk in chunks:
            self.chunks[
                (payload["record_id"], payload["revision_id"], chunk["chunk_id"])
            ] = body[chunk["char_start"] : chunk["char_end"]]
        return {
            "chunks": chunks,
            "content_sha256": payload["content_sha256"],
            "hash_version": payload["hash_version"],
            "record_id": payload["record_id"],
            "revision_id": payload["revision_id"],
        }

    async def erase(self, record_id: str, revision_id: str) -> dict:
        for key in [key for key in self.chunks if key[0] == record_id and key[1] == revision_id]:
            del self.chunks[key]
        return {"record_id": record_id, "revision_id": revision_id, "erased": True}

    # -- ranking half -----------------------------------------------------

    async def search(self, request: RetrievalRequest) -> list[RetrievalReference]:
        terms = {term for term in request.query.lower().split() if len(term) > 2}
        if not terms:
            return []
        scored = []
        for (record_id, revision_id, chunk_id), text in self.chunks.items():
            overlap = terms & set(text.lower().split())
            if overlap:
                scored.append((len(overlap) / len(terms), record_id, revision_id, chunk_id))
        scored.sort(key=lambda row: (-row[0], row[1], row[2], row[3]))
        return [
            RetrievalReference(
                record_id=record_id,
                revision_id=revision_id,
                chunk_id=chunk_id,
                score=round(score, 6),
                provider_version=self.provider_version,
            )
            for score, record_id, revision_id, chunk_id in scored[: request.limit]
        ]


def _label_of(item: dict, corpus: Corpus) -> str | None:
    for label, revision in corpus.revisions.items():
        if item["record_id"] == revision["record_id"]:
            return label
    return None


def _arm_result(result: dict, corpus: Corpus) -> list[dict]:
    return list(result.get("items", []))


def _metrics(
    selections: list[list[dict]], corpus: Corpus, latency_ms: float, *, fallbacks: int = 0
) -> dict:
    selected = [item for items in selections for item in items]
    labels = [_label_of(item, corpus) for item in selected]
    eligible = [label for label in labels if label is not None and label not in corpus.ineligible]
    required_total = sum(len(scenario.required) for scenario in SCENARIOS)
    required_hit = sum(
        1
        for scenario, items in zip(SCENARIOS, selections, strict=True)
        for label in scenario.required
        if any(_label_of(item, corpus) == label for item in items)
    )
    exact = sum(
        1
        for item in selected
        if (revision := corpus.revisions.get(_label_of(item, corpus) or ""))
        and item["revision_id"] == revision["revision_id"]
    )
    expects_empty = [scenario for scenario in SCENARIOS if scenario.expects_empty]
    empty_hits = sum(
        1
        for scenario, items in zip(SCENARIOS, selections, strict=True) if not items
    )
    # Duplicates are the same revision twice in one selection, not the same
    # evidence answering two different queries.
    duplicates = sum(
        len(items) - len({(item["record_id"], item["revision_id"]) for item in items})
        for items in selections
    )
    return {
        "precision_at_8": round(len(eligible) / len(selected), 4) if selected else 1.0,
        "required_evidence_recall": (
            round(required_hit / required_total, 4) if required_total else 1.0
        ),
        "exact_citation_rate": round(exact / len(selected), 4) if selected else 1.0,
        "ineligible_selection_rate": (
            round(1 - len(eligible) / len(labels), 4) if labels else 0.0
        ),
        "empty_bundle_accuracy": (
            round(empty_hits / len(expects_empty), 4) if expects_empty else 1.0
        ),
        "duplicate_rate": round(duplicates / len(selected), 4) if selected else 0.0,
        "added_input_tokens_upper_bound": sum(
            len(f"{item['title']}{item['summary'] or ''}".encode()) for item in selected
        ),
        "latency_ms": round(latency_ms, 3),
        "lexical_fallbacks": fallbacks,
        "selected_records": len(selected),
        "scenarios": len(SCENARIOS),
    }


def protected_failures(selections: list[list[dict]], corpus: Corpus) -> list[dict]:
    """Any protected scenario that served a forbidden or ineligible identity."""

    failures = []
    for scenario, items in zip(SCENARIOS, selections, strict=True):
        if not scenario.protected:
            continue
        labels = {_label_of(item, corpus) for item in items}
        forbidden = sorted(
            (labels & set(scenario.forbidden)) | (labels & corpus.ineligible) - {None}
        )
        leaked = sorted(label for label in labels if label not in corpus.revisions)
        if forbidden or leaked:
            failures.append({"scenario": scenario.scenario_id, "served": forbidden + leaked})
        if scenario.expects_empty and items:
            failures.append({"scenario": scenario.scenario_id, "served": ["unexpected_selection"]})
    return failures


async def compare_arms(corpus: Corpus, lexical, retrieval, principal, project_id) -> dict:
    """Run all declared arms over the shared corpus and report the metrics.

    ``lexical`` is a callable returning a core search result; ``retrieval`` is
    the :class:`KnowledgeRetrieval` boundary. Both are supplied so the caller
    keeps ownership of configuration and authorization.
    """

    async def run(arm) -> tuple[list[list[dict]], float, int]:
        selections, fallbacks = [], 0
        started = time.perf_counter()
        for scenario in SCENARIOS:
            if arm == "no_memory":
                selections.append([])
                continue
            if arm == "lexical":
                result = await lexical(query=scenario.query, limit=TOP_K)
            else:
                result = await retrieval.search(
                    principal=principal,
                    project_id=project_id,
                    query=scenario.query,
                    limit=TOP_K,
                    semantic=True,
                )
                if result.get("retrieval", {}).get("mode") != "semantic":
                    # An absent index, an outage or an ineligible hit answers from
                    # a fresh lexical read; the count keeps that degradation visible.
                    fallbacks += 1
            selections.append(_arm_result(result, corpus))
        elapsed = (time.perf_counter() - started) * 1000 / len(SCENARIOS)
        return selections, elapsed, fallbacks

    arms, failures = {}, []
    for arm in ARMS:
        selections, latency_ms, fallbacks = await run(arm)
        arms[arm] = _metrics(selections, corpus, latency_ms, fallbacks=fallbacks)
        if arm != "no_memory":
            failures.extend(protected_failures(selections, corpus))
    lexical_metrics, semantic_metrics = arms["lexical"], arms["semantic"]
    comparisons = {
        "required_evidence_recall_not_lower": (
            semantic_metrics["required_evidence_recall"]
            >= lexical_metrics["required_evidence_recall"]
        ),
        "precision_at_8_not_lower": (
            semantic_metrics["precision_at_8"] >= lexical_metrics["precision_at_8"]
        ),
        "zero_protected_failures": not failures,
    }
    return {
        "schema_version": 1,
        "measurement": "offline_contract",
        "model_quality": "unmeasured",
        "source_class": "synthetic",
        "corpus": {"records": len(corpus.labels), "ineligible": sorted(corpus.ineligible)},
        "scenarios": [
            {
                "scenario_id": scenario.scenario_id,
                "query": scenario.query,
                "required": list(scenario.required),
                "forbidden": list(scenario.forbidden),
                "expects_empty": scenario.expects_empty,
                "protected": scenario.protected,
            }
            for scenario in SCENARIOS
        ],
        "arms": arms,
        "declared_metrics": list(DECLARED_METRICS),
        "comparisons": comparisons,
        "protected_failures": failures,
        "g5_satisfied": False,
        "g5_missing_evidence": list(G5_MISSING_EVIDENCE),
    }


def retrieval_boundary(service, provider, *, memory_enabled=True, timeout_seconds=5):
    """A semantic boundary wired to one registered offline provider."""

    return KnowledgeRetrieval(
        service,
        MemoryConfig(enabled=memory_enabled),
        provider_lookup=RetrievalProviderRegistry(lambda name: provider).lookup,
        timeout_seconds=timeout_seconds,
    )


async def index_corpus(receipts: DerivedIndexReceipts, provider, entries, principal, project_id):
    """Hydrate, index and acknowledge every labeled revision, in order.

    A record that is not acknowledged has no chunk the semantic arm can serve,
    so an arm's reach is exactly its acknowledged index.
    """

    acknowledged = []
    for label, record in entries:
        payload = await receipts.hydrate_index_payload(
            record_id=record["record_id"], revision_id=record["revision_id"],
            principal=principal, project_id=project_id,
        )
        manifest = await provider.index(payload)
        result = await receipts.acknowledge(
            provider_id=provider.provider_id, manifest=manifest,
            principal=principal, project_id=project_id,
        )
        acknowledged.append({"label": label, **result})
    return acknowledged
