"""The legacy aq-memory adapter port: scope isolation and summary suppression.

These are the legacy scope and summary behaviours, ported onto the core ranking
contract: a legacy search result may only become a reference for an exact
record/revision/chunk in the requested scope, and its summary, original, content
and tag fields never survive the conversion. No plugin is installed and no
Milvus, embedding or network path exists here.
"""

from contextlib import contextmanager

import pytest

from src.commands.principal import TRUSTED_LOCAL
from src.config import MemoryConfig
from src.knowledge.memory_port import MemorySemanticPort, legacy_adapter
from src.knowledge.providers import KnowledgeRetrieval, RetrievalReference, RetrievalRequest
from src.knowledge.registration import RetrievalProviderRegistry
from src.knowledge.service import KnowledgeService
from src.records.models import RecordError
from tests.record_helpers import knowledge_config, seed_project, snapshot

LOCAL = {"principal": TRUSTED_LOCAL, "project_id": "p"}
RECORD = "11111111-1111-4111-8111-111111111111"
REVISION = "22222222-2222-4222-8222-222222222222"
OTHER_REVISION = "33333333-3333-4333-8333-333333333333"


@contextmanager
def refuses(code):
    with pytest.raises(RecordError) as excinfo:
        yield
    assert excinfo.value.code == code


class LegacyMemoryService:
    """The legacy aq-memory surface: scoped rows with summary and original text.

    Every write path is a spy that fails the test if the adapter reaches it.
    """

    def __init__(self, rows, *, available=True):
        self.rows = rows
        self.available = available
        self.calls = []
        self.writes = []

    async def search(self, project_id, query, *, scope=None, topic=None, top_k=10):
        self.calls.append(
            {"project_id": project_id, "query": query, "scope": scope, "top_k": top_k}
        )
        return list(self.rows)[:top_k]

    def _forbidden(self, name):
        async def refuse(*args, **kwargs):
            self.writes.append(name)
            raise AssertionError(f"the adapter called an authoritative write: {name}")

        return refuse

    def __getattr__(self, name):
        if name in {
            "kv_set",
            "fact_set",
            "save_document",
            "update_document_content",
            "consolidate",
            "promote",
            "delete",
        }:
            return self._forbidden(name)
        raise AttributeError(name)


def row(**changes):
    value = {
        "record_id": RECORD,
        "revision_id": REVISION,
        "chunk_id": "chunk-1",
        "score": 0.82,
        "content": "PLUGIN CONTENT BYTES",
        "summary": "PLUGIN SUMMARY",
        "original": "PLUGIN ORIGINAL",
        "tags": ["secret-tag"],
    }
    value.update(changes)
    return value


def request(**changes):
    value = {"query": "deploy procedure", "scope_key": "project:p",
             "include_shared_global": True, "limit": 8}
    value.update(changes)
    return RetrievalRequest(**value)


def port_for(rows, **kwargs):
    memory = LegacyMemoryService(rows, **kwargs)
    return legacy_adapter(memory, provider_version="v2"), memory


# --- scope ----------------------------------------------------------------


async def test_the_core_scope_is_forwarded_as_an_opaque_label():
    port, memory = port_for([])
    await port.search(request(scope_key="project:p"))
    assert memory.calls == [
        {
            "project_id": "project:p",
            "query": "deploy procedure",
            "scope": "project:p",
            "top_k": 8,
        }
    ]


async def test_a_foreign_scoped_row_is_dropped_rather_than_trusted():
    rows = [row(), row(chunk_id="chunk-2", scope_key="project:q"), row(chunk_id="chunk-3",
         scope_key="global")]
    port, _ = port_for(rows)
    references = await port.search(request())
    assert [reference.chunk_id for reference in references] == ["chunk-1"]


async def test_an_unscoped_row_is_kept_but_reauthorized_by_core_later():
    port, _ = port_for([row(scope_key=None), row(chunk_id="chunk-2", scope_key="project:p")])
    assert len(await port.search(request())) == 2


# --- summary and text -----------------------------------------------------


async def test_legacy_summary_original_and_content_never_survive():
    port, _ = port_for([row()])
    (reference,) = await port.search(request())
    assert isinstance(reference, RetrievalReference)
    assert reference.model_dump(mode="json") == {
        "record_id": RECORD,
        "revision_id": REVISION,
        "chunk_id": "chunk-1",
        "score": 0.82,
        "provider_version": "v2",
    }
    assert not any(
        isinstance(value, str) and value.startswith("PLUGIN")
        for value in reference.model_dump().values()
    )


async def test_the_adapter_exposes_no_write_path_and_writes_nothing():
    port, memory = port_for([row()])
    for name in ("kv_set", "fact_set", "save_document", "consolidate", "promote", "delete"):
        assert not hasattr(port, name)
    await port.search(request())
    assert memory.writes == []


async def test_rows_without_exact_identities_are_not_knowledge_references():
    rows = [
        {"content": "a legacy KV value", "score": 0.9},
        {"record_id": RECORD, "score": 0.9},
        {"record_id": RECORD, "revision_id": REVISION, "score": 0.9},
        {"record_id": RECORD, "revision_id": "not-a-uuid", "chunk_id": "c", "score": 0.5},
        {"record_id": RECORD, "revision_id": REVISION, "chunk_id": "c", "score": "high"},
        {"record_id": RECORD, "revision_id": REVISION, "chunk_id": "c", "score": float("inf")},
        row(),
    ]
    port, _ = port_for(rows)
    assert [reference.chunk_id for reference in await port.search(request())] == ["chunk-1"]


async def test_duplicate_chunks_collapse_and_the_result_is_bounded():
    rows = [row(), row(), row(chunk_id="chunk-2"), row(chunk_id="chunk-3"), row(chunk_id="c4")]
    port, memory = port_for(rows)
    references = await port.search(request(limit=3))
    assert [reference.chunk_id for reference in references] == ["chunk-1", "chunk-2"]
    assert memory.calls[0]["top_k"] == 3


@pytest.mark.parametrize("limit", [0, -1, 101, True, "8"])
async def test_an_unbounded_limit_is_refused_before_the_plugin_is_called(limit):
    port, memory = port_for([row()])
    with refuses("record.invalid_input"):
        await port.search(request(limit=limit))
    assert memory.calls == []


async def test_a_provider_supplied_version_is_bounded_and_never_trusted_blind():
    port, _ = port_for([row(provider_version="plugin-supplied")])
    (reference,) = await port.search(request())
    assert reference.provider_version == "plugin-supplied"
    port, _ = port_for([row(provider_version="x" * 129)])
    assert await port.search(request()) == []


async def test_a_malformed_legacy_result_is_a_core_error_not_a_silent_empty():
    class Malformed:
        available = True

        async def search(self, project_id, query, *, scope=None, topic=None, top_k=10):
            return {"not": "a list of rows"}

    port = legacy_adapter(Malformed(), provider_version="v2")
    with refuses("record.invalid_input"):
        await port.search(request())


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_version": ""},
        {"provider_version": "v" * 129},
        {"provider_id": "A B"},
        {"provider_id": ""},
    ],
)
def test_the_adapter_refuses_an_unusable_identity(changes):
    with refuses("knowledge.provider_invalid_id"):
        legacy_adapter(LegacyMemoryService([]), **{"provider_version": "v2", **changes})


def test_the_adapter_refuses_an_unusable_top_k_cap():
    with refuses("record.invalid_input"):
        MemorySemanticPort(
            LegacyMemoryService([]), provider_id="aq-memory", provider_version="v2", top_k_cap=0
        )


def test_the_handshake_is_versioned_read_only_and_says_no_writes():
    port, _ = port_for([])
    assert port.handshake() == {
        "adapter": "legacy-memory-semantic",
        "handshake_version": 1,
        "provider_id": "aq-memory",
        "provider_version": "v2",
        "provider_name": "aq-memory",
        "read_only": True,
        "authoritative_writes": False,
        "extraction_fenced": True,
    }
    assert port.available is True
    unavailable, _ = port_for([], available=False)
    assert unavailable.available is False


# --- through the core boundary -------------------------------------------


@pytest.fixture
async def service(reuse_database):
    db = await reuse_database()
    await seed_project(db)
    await seed_project(db, "q")
    config = knowledge_config(global_enabled=True)
    config.semantic.enabled = True
    return KnowledgeService(db, config)


def boundary(service, memory):
    port = legacy_adapter(memory, provider_version="v2")
    return KnowledgeRetrieval(
        service,
        MemoryConfig(enabled=True),
        provider_lookup=RetrievalProviderRegistry(lambda name: port).lookup,
        timeout_seconds=5,
    )


async def test_an_authorized_semantic_search_serves_core_summaries_only(service):
    record = await service.create(
        snapshot=snapshot(title="Deploy procedure", summary="Core summary", body="Health gate."),
        idempotency_key="record",
        **LOCAL,
    )
    memory = LegacyMemoryService(
        [row(record_id=record["record_id"], revision_id=record["revision_id"])]
    )
    result = await boundary(service, memory).search(query="deploy", semantic=True, **LOCAL)
    assert result["retrieval"] == {"mode": "semantic", "reason": "provider_references"}
    (item,) = result["items"]
    assert item["summary"] == "Core summary"
    assert item["ranking"]["provider_version"] == "v2"
    assert "PLUGIN" not in str(result)


async def test_an_unavailable_or_absent_adapter_falls_back_to_lexical(service):
    record = await service.create(
        snapshot=snapshot(title="Deploy procedure", body="Health gate."),
        idempotency_key="record",
        **LOCAL,
    )
    unavailable = LegacyMemoryService([row()], available=False)
    result = await boundary(service, unavailable).search(query="deploy", semantic=True, **LOCAL)
    assert result["retrieval"] == {"mode": "lexical", "reason": "provider_absent"}
    assert [item["record_id"] for item in result["items"]] == [record["record_id"]]

    absent = KnowledgeRetrieval(
        service, MemoryConfig(enabled=True), provider_lookup=lambda: None, timeout_seconds=5
    )
    again = await absent.search(query="deploy", semantic=True, **LOCAL)
    assert again["retrieval"] == {"mode": "lexical", "reason": "provider_absent"}


async def test_a_legacy_foreign_revision_never_becomes_a_served_summary(service):
    public = await service.create(
        snapshot=snapshot(title="Deploy procedure", body="Health gate."),
        idempotency_key="public",
        **LOCAL,
    )
    private = await service.create(
        snapshot=snapshot(title="PRIVATE TITLE", body="PRIVATE BODY"),
        idempotency_key="private",
        principal=TRUSTED_LOCAL,
        project_id="q",
    )
    memory = LegacyMemoryService(
        [
            row(record_id=private["record_id"], revision_id=private["revision_id"]),
            row(
                record_id=public["record_id"],
                revision_id=OTHER_REVISION,
                chunk_id="chunk-2",
            ),
        ]
    )
    result = await boundary(service, memory).search(
        query="nothing lexically matches", semantic=True, **LOCAL
    )
    assert result["retrieval"]["mode"] == "lexical"
    assert result["items"] == []
    assert "PRIVATE" not in str(result)


async def test_a_malformed_legacy_result_falls_back_instead_of_failing_the_read(service):
    await service.create(
        snapshot=snapshot(title="Deploy procedure", body="Health gate."),
        idempotency_key="record",
        **LOCAL,
    )
    class Malformed:
        available = True

        async def search(self, project_id, query, *, scope=None, topic=None, top_k=10):
            return {"not": "a list of rows"}

    result = await boundary(service, Malformed()).search(query="deploy", semantic=True, **LOCAL)
    assert result["retrieval"] == {"mode": "lexical", "reason": "provider_unavailable"}
    assert result["items"]


def test_the_port_satisfies_the_registered_provider_shape():
    port, _ = port_for([])
    registry = RetrievalProviderRegistry(lambda name: port)
    assert registry.lookup() is port
    entry = registry.require("aq-memory")
    assert entry.handshake()["authoritative_writes"] is False
    assert isinstance(port, MemorySemanticPort)
