"""Inert defaults and degraded optional retrieval preserve ordinary core reads."""

import asyncio
import importlib
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from src.commands.principal import TRUSTED_LOCAL
from src.config import MemoryConfig
from src.knowledge.providers import KnowledgeRetrieval
from src.knowledge.service import KnowledgeService
from src.records.models import RecordError
from tests.record_helpers import knowledge_config, seed_project, snapshot

LOCAL = dict(principal=TRUSTED_LOCAL, project_id="p")


@pytest.fixture
async def service(reuse_database):
    db = await reuse_database()
    await seed_project(db)
    service = KnowledgeService(db, knowledge_config())
    await service.create(
        snapshot=snapshot(title="Lexical evidence", body="Exact evidence"),
        idempotency_key="create",
        **LOCAL,
    )
    return service


async def assert_lexical_and_exact(boundary, reason):
    expected = await boundary.service.search(query="evidence", **LOCAL)
    result = await boundary.search(query="evidence", semantic=True, **LOCAL)
    assert result == {**expected, "retrieval": {"mode": "lexical", "reason": reason}}
    item = result["items"][0]
    shown = await boundary.read(
        dict(
            record_id=item["record_id"],
            revision_id=item["revision_id"],
            chunk_id="opaque-index",
            provider_version="fake",
            score=1.0,
        ),
        **LOCAL,
    )
    assert shown["snapshot"]["body"] == "Exact evidence"


async def test_absent_provider_preserves_lexical_and_explicit_reads(service):
    service.config.semantic.enabled = True
    boundary = KnowledgeRetrieval(service, MemoryConfig(enabled=True))
    await assert_lexical_and_exact(boundary, "provider_absent")


async def test_absent_registration_preserves_reads_without_initialization(service):
    service.config.semantic.enabled = True
    lookup = Mock(return_value=None)
    boundary = KnowledgeRetrieval(service, MemoryConfig(enabled=True), provider_lookup=lookup)
    await assert_lexical_and_exact(boundary, "provider_absent")
    lookup.assert_called_once_with()


@pytest.mark.parametrize("disabled", ["semantic", "memory", "both"])
async def test_disabled_switches_never_lookup_initialize_or_network(service, monkeypatch, disabled):
    import socket
    from src.plugins.registry import PluginRegistry

    initialize = AsyncMock(side_effect=AssertionError("plugin initialization"))
    network = Mock(side_effect=AssertionError("external network"))
    lookup = Mock(side_effect=AssertionError("provider lookup"))
    monkeypatch.setattr(PluginRegistry, "load_plugin", initialize)
    monkeypatch.setattr(socket, "create_connection", network)
    service.config.semantic.enabled = disabled == "memory"
    boundary = KnowledgeRetrieval(
        service, MemoryConfig(enabled=disabled == "semantic"), provider_lookup=lookup
    )
    await assert_lexical_and_exact(boundary, "semantic_disabled")
    lookup.assert_not_called()
    initialize.assert_not_awaited()
    network.assert_not_called()


async def test_storage_disabled_never_looks_up_provider(service):
    service.config.enabled = False
    service.config.semantic.enabled = True
    lookup = Mock(side_effect=AssertionError("provider initialization"))
    boundary = KnowledgeRetrieval(service, MemoryConfig(enabled=True), provider_lookup=lookup)
    with pytest.raises(RecordError, match="knowledge.disabled"):
        await boundary.search(semantic=True, **LOCAL)
    lookup.assert_not_called()


async def test_lexical_default_and_exact_read_never_lookup_even_when_enabled(service):
    service.config.semantic.enabled = True
    lookup = Mock(side_effect=AssertionError("unexpected optional work"))
    boundary = KnowledgeRetrieval(service, MemoryConfig(enabled=True), provider_lookup=lookup)
    result = await boundary.search(query="evidence", **LOCAL)
    assert result["retrieval"]["reason"] == "lexical_requested"
    record = result["items"][0]
    await boundary.read(
        dict(
            record_id=record["record_id"],
            revision_id=record["revision_id"],
            chunk_id="old-chunk",
            score=1.0,
            provider_version="v0",
        ),
        **LOCAL,
    )
    lookup.assert_not_called()


@pytest.mark.parametrize("failure", ["lookup", "search", "timeout", "oversized", "text", "stale"])
async def test_provider_failures_preserve_fresh_lexical_and_exact_reads(service, failure):
    service.config.semantic.enabled = True
    reference = dict(
        record_id=str(uuid4()),
        revision_id=str(uuid4()),
        chunk_id="obsolete",
        score=0.5,
        provider_version="v0",
    )
    provider = Mock(search=AsyncMock(return_value=[reference]))
    lookup = Mock(return_value=provider)
    expected = "no_eligible_references"
    if failure == "lookup":
        lookup.side_effect = RuntimeError("PRIVATE INITIALIZATION DETAILS")
        expected = "provider_unavailable"
    elif failure == "search":
        provider.search.side_effect = RuntimeError("PRIVATE PROVIDER DETAILS")
        expected = "provider_unavailable"
    elif failure == "timeout":

        async def timeout(_request):
            await asyncio.Event().wait()

        provider.search.side_effect = timeout
        expected = "provider_timeout"
    elif failure == "oversized":
        provider.search.return_value = [reference] * 26
        expected = "provider_invalid_result"
    elif failure == "text":
        provider.search.return_value = {"text": "PRIVATE SNIPPET"}
        expected = "provider_invalid_result"
    boundary = KnowledgeRetrieval(
        service, MemoryConfig(enabled=True), provider_lookup=lookup, timeout_seconds=0.02
    )
    await assert_lexical_and_exact(boundary, expected)


async def test_disable_during_provider_search_returns_lexical(service):
    service.config.semantic.enabled = True
    provider = Mock(search=AsyncMock())

    async def disabled(_request):
        service.config.semantic.enabled = False
        return []

    provider.search.side_effect = disabled
    boundary = KnowledgeRetrieval(
        service, MemoryConfig(enabled=True), provider_lookup=lambda: provider
    )
    await assert_lexical_and_exact(boundary, "semantic_disabled")


@pytest.mark.parametrize(
    "limit,query,semantic",
    [(101, "", True), (0, "", True), (True, "", True), (25, "x" * 4097, True), (25, "", "yes")],
    ids=["too-many", "zero-limit", "bool-limit", "long-query", "invalid-semantic"],
)
async def test_invalid_requests_fail_before_optional_work(service, limit, query, semantic):
    service.config.semantic.enabled = True
    lookup = Mock(side_effect=AssertionError("optional initialization"))
    boundary = KnowledgeRetrieval(service, MemoryConfig(enabled=True), provider_lookup=lookup)
    with pytest.raises(RecordError) as exc:
        await boundary.search(limit=limit, query=query, semantic=semantic, **LOCAL)
    assert exc.value.code == "record.invalid_input"
    lookup.assert_not_called()


def test_import_has_no_optional_initialization_or_transport(monkeypatch):
    import socket
    import src.knowledge.providers as module
    from src.plugins.registry import PluginRegistry

    initialize = AsyncMock(side_effect=AssertionError("initialization on import"))
    network = Mock(side_effect=AssertionError("network on import"))
    monkeypatch.setattr(PluginRegistry, "load_plugin", initialize)
    monkeypatch.setattr(socket, "create_connection", network)
    importlib.reload(module)
    initialize.assert_not_awaited()
    network.assert_not_called()


@pytest.mark.parametrize("timeout", [0, -1, 6, True, float("nan"), float("inf")])
def test_invalid_timeout_is_rejected(timeout):
    with pytest.raises(ValueError):
        KnowledgeRetrieval(Mock(), MemoryConfig(), timeout_seconds=timeout)
