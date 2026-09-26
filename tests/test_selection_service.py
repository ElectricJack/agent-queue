"""Selection orchestration with real git/PostgreSQL and an offline Jev transport."""

import asyncio
from dataclasses import asdict, replace

import pytest
import yaml

from src.config import AppConfig, TestSelectionConfig as SelectionConfig, load_config
from src.database import Database
from src.git.manager import GitManager
from src.models import Project
from src.test_selection.catalogue import build_catalogue, load_areas, render_catalogue
from src.test_selection.service import SelectionRequest, SelectionService, select_offline
from src.test_selection.static_impact import FixedStaticImpact, UnavailableStaticImpact
from src.test_selection.typesafe import FakeTransport, TransportError, TransportResponse
from tests.db_fixtures import lease_dsn
from tests.selection_fixture_repo import build_fixture_repo, git


@pytest.fixture
def repo(tmp_path):
    return build_fixture_repo(tmp_path / "repo")


@pytest.fixture
async def db():
    database = Database(lease_dsn("test-selection-service"))
    await database.initialize()
    try:
        for pid in ("p", "q"):
            await database.create_project(Project(id=pid, name=pid))
        yield database
    finally:
        await database.close()


def request(repo, **changes):
    return replace(
        SelectionRequest("p", None, None, None, str(repo), "shadow", None, (), True),
        **changes,
    )


def response(*, all_unaffected=False):
    answers = {}
    for area in ("alpha", "beta", "gamma", "docs_guards"):
        choice = (
            "unaffected"
            if all_unaffected or area == "beta"
            else ("affected" if area == "alpha" else "unknown")
        )
        probabilities = {key: float(key == choice) for key in ("affected", "unaffected", "unknown")}
        answers["area_" + area] = {
            "type": "choice",
            "choice": choice,
            "probabilities": probabilities,
            "confidence": 1.0,
        }
    return TransportResponse("jev-1.13.0", answers, 318, 12)


def service(db, *, transport=None, config=None, static=None):
    async def default_branch(project_id):
        return "main"

    return SelectionService(
        db=db,
        git=GitManager(),
        config_getter=lambda: (
            config or SelectionConfig(enabled=True, jev_enabled=True, enforce_enabled=True)
        ),
        static=static or FixedStaticImpact({"tests/test_a.py", "tests/sub/test_b.py"}),
        transport_factory=lambda config: transport,
        default_branch_getter=default_branch,
    )


def modify(repo, path="src/pkg/a.py", text="def alpha():\n    return 2\n"):
    (repo / path).write_text(text)


async def promote(db, record):
    return await db.insert_test_selection_promotion(
        {
            "project_id": record["project_id"],
            "model": record["jev_requested_model"],
            "question_schema_version": record["question_schema_version"],
            **{key: record[key] for key in ("catalogue_digest", "rules_digest", "policy_digest")},
            "evidence": {"recall": 1.0},
            "promoted_by": "operator",
            "promoted_at": 1.0,
        }
    )


async def test_clean_disabled_and_each_invocation_is_persisted(db, repo):
    selector = service(db, static=FixedStaticImpact(()))
    record = await selector.select(request(repo, jev=False))
    assert record["recorded"] is True
    assert record["jev_status"] == "disabled"
    assert record["fallback_modules"] == record["final_modules"] == []
    assert record["full_required"] is False
    assert record["argv"] == [[]]
    assert sum(o["kind"] == "marker_arm" for o in record["pending_obligations"]) == 2
    assert await db.get_test_selection(record["id"]) == {
        key: value for key, value in record.items() if key != "recorded"
    }


async def test_composition_and_cache_record_independent_invocations(db, repo):
    modify(repo)
    transport = FakeTransport(response())
    selector = service(db, transport=transport)
    first = await selector.select(request(repo))
    second = await selector.select(request(repo))
    assert first["mandatory_modules"] == ["tests/test_a.py"]
    assert first["static_modules"] == ["tests/sub/test_b.py", "tests/test_a.py"]
    assert first["jev_modules"] == ["tests/test_a.py", "tests/test_c.py", "tests/test_docs_scan.py"]
    assert first["final_modules"] == first["jev_modules"]
    assert first["area_decisions"]["beta"]["omitted"] is True
    assert first["jev_status"] == "ok"
    assert first["usage"]["input_tokens"] == 318
    assert len(transport.calls) == 1
    assert "jev_cached" in second["usage"]["reasons"]
    assert first["id"] != second["id"]
    for key in ("catalogue_digest", "rules_digest", "policy_digest", "cache_key"):
        assert first[key].startswith("sha256:")


async def test_promotion_matches_all_artifacts_and_is_checked_on_cache_hits(db, repo):
    modify(repo)
    selector = service(db, transport=FakeTransport(response(all_unaffected=True)))
    req = request(repo, mode="enforce")
    first = await selector.select(req)
    assert first["fallback_reason"] == "not_promoted"
    promotion = await promote(db, first)
    second = await selector.select(req)
    assert second["promotion_id"] == promotion["id"]
    assert second["jev_used_for_omission"] is True
    assert second["final_modules"] == ["tests/test_a.py"]
    assert await db.revoke_test_selection_promotion(promotion["id"], now=2, reason="revoke")
    third = await selector.select(req)
    assert third["jev_used_for_omission"] is False
    assert third["final_modules"] == third["fallback_modules"]


async def test_area_change_invalidates_cache_and_promotion(db, repo):
    # The small fixture predates artifact ownership; explicitly map these
    # changes so this case exercises digest identity rather than unmapped paths.
    rules = repo / "tests/selection_rules.yaml"
    rules.write_text(rules.read_text().replace('  - "*.md"', '  - "*.md"\n  - "tests/selection_*"'))
    git(repo, "add", "tests/selection_rules.yaml")
    git(repo, "commit", "-qm", "map fixture selection artifacts")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    modify(repo)
    transport = FakeTransport(response(all_unaffected=True), response(all_unaffected=True))
    selector = service(db, transport=transport)
    first = await selector.select(request(repo, mode="enforce"))
    await promote(db, first)
    path = repo / "tests/selection_areas.yaml"
    path.write_text(path.read_text().replace("returns one.", "returns its value."))
    catalogue = build_catalogue(repo, load_areas(path))
    (repo / "tests/selection_catalogue.json").write_text(render_catalogue(catalogue))
    second = await selector.select(request(repo, mode="enforce"))
    assert second["catalogue_digest"] != first["catalogue_digest"]
    assert len(transport.calls) == 2
    assert second["fallback_reason"] == "not_promoted"
    assert second["jev_used_for_omission"] is False


async def test_http_failure_is_never_cached_or_used_for_omission(db, repo):
    modify(repo)
    transport = FakeTransport(TransportError("http_429"), response(all_unaffected=True))
    selector = service(db, transport=transport)
    first = await selector.select(request(repo, mode="enforce"))
    assert first["jev_status"] == "unavailable"
    assert first["fallback_reason"] == "fallback_http_429"
    assert first["final_modules"] == first["fallback_modules"]
    await promote(db, first)
    second = await selector.select(request(repo, mode="enforce"))
    assert len(transport.calls) == 2
    assert second["jev_used_for_omission"] is True


@pytest.mark.parametrize("invalidator", ["pyproject.toml", "tests/conftest.py"])
async def test_global_invalidator_skips_network(db, repo, invalidator):
    modify(repo, invalidator, (repo / invalidator).read_text() + "\n# change\n")
    transport = FakeTransport()
    record = await service(db, transport=transport).select(request(repo))
    assert record["full_required"] is True
    assert len(record["final_modules"]) == 4
    assert record["final_modules"] == record["mandatory_modules"]
    assert transport.calls == []


async def test_explicit_targets_and_pending_obligations(db, repo):
    modify(repo)
    (repo / "dashboard").mkdir()
    (repo / "dashboard/x.ts").write_text("export const x = 1;\n")
    req = request(
        repo,
        targets=("tests/sub/test_b.py", "tests/test_a.py::test_alpha", "elsewhere/"),
        acceptance_commands=("aq test tests/test_a.py -k x",),
    )
    record = await service(db, transport=FakeTransport()).select(req)
    assert "tests/sub/test_b.py" in record["final_modules"]
    assert "explicit_target" in record["reasons"]["tests/sub/test_b.py"]
    assert all("::" not in target for target in record["argv"][0])
    assert "elsewhere/" in record["argv"][0]
    obligations = record["pending_obligations"]
    assert {"kind": "frontend", "command": "npm --prefix dashboard test"} in obligations
    assert {"kind": "acceptance", "command": "aq test tests/test_a.py -k x"} in obligations
    assert any(
        o["kind"] == "explicit_target" and o["target"].endswith("::test_alpha") for o in obligations
    )


async def test_recheck_detects_change_and_revert_and_observe_appends(db, repo):
    selector = service(db)
    record = await selector.select(request(repo, jev=False))
    assert not (await selector.recheck(record["id"]))["stale"]
    original = (repo / "src/pkg/a.py").read_text()
    modify(repo)
    assert (await selector.recheck(record["id"]))["stale"]
    modify(repo, text=original)
    assert not (await selector.recheck(record["id"]))["stale"]
    row = await selector.observe(
        record["id"],
        kind="execution",
        source="worker",
        exit_code=1,
        duration_ms=25,
        executed_modules=["tests/test_a.py"],
        failed_node_ids=["tests/test_a.py::test_alpha"],
        payload={},
    )
    assert await db.list_test_selection_observations(record["id"]) == [row]
    assert await db.get_test_selection(record["id"]) == {
        key: value for key, value in record.items() if key != "recorded"
    }
    with pytest.raises(LookupError):
        await selector.recheck("unknown")


@pytest.mark.parametrize("broken", ["missing", "stale", "bad_rules", "bad_policy"])
async def test_unusable_artifacts_record_full_fallback_without_guessed_universe(db, repo, broken):
    if broken == "missing":
        (repo / "tests/selection_catalogue.json").unlink()
    elif broken == "stale":
        modify(repo, "tests/test_c.py", "def test_new():\n    pass\n")
    else:
        modify(
            repo,
            "tests/selection_" + ("rules" if broken == "bad_rules" else "policy") + ".yaml",
            "version: 999\n",
        )
    transport = FakeTransport()
    record = await service(db, transport=transport).select(request(repo))
    assert record["full_required"] is True
    assert record["fallback_reason"] == "catalogue_unusable"
    assert record["final_modules"] == []
    assert record["argv"] == [["tests/"]]
    assert transport.calls == []


async def test_static_failure_and_unconfigured_transport_are_distinct(db, repo):
    modify(repo)
    transport = FakeTransport()
    broad = await service(db, static=UnavailableStaticImpact(), transport=transport).select(
        request(repo)
    )
    assert broad["full_required"] is True
    assert broad["fallback_reason"] == "static_unavailable"
    assert transport.calls == []
    unconfigured = await service(db).select(request(repo))
    assert unconfigured["jev_status"] == "unconfigured"
    assert unconfigured["fallback_reason"] == "jev_unconfigured"


async def test_cache_is_project_scoped_and_bounded(db, repo):
    modify(repo)
    transport = FakeTransport(*(response() for _ in range(3)))
    selector = service(
        db,
        transport=transport,
        config=SelectionConfig(enabled=True, jev_enabled=True, cache_entries=1),
    )
    first = await selector.select(request(repo))
    other = await selector.select(request(repo, project_id="q"))
    again = await selector.select(request(repo))
    assert first["cache_key"] != other["cache_key"]
    assert len(transport.calls) == 3
    assert "jev_cached" not in again["usage"]["reasons"]


async def test_unpersisted_service_path_does_not_insert(db, repo):
    record = await service(db).select(request(repo, jev=False), persist=False)
    assert record["recorded"] is False and "id" not in record
    assert await db.list_test_selections(project_id="p") == []


async def test_truncated_evidence_blocks_promoted_omission_and_changes_cache_identity(db, repo):
    modify(repo, text="def alpha():\n" + "    # extra evidence\n" * 5 + "    return 2\n")
    config = SelectionConfig(enabled=True, jev_enabled=True, enforce_enabled=True, excerpt_lines=2)
    transport = FakeTransport(response(all_unaffected=True), response(all_unaffected=True))
    selector = service(db, config=config, transport=transport)
    req = request(repo, mode="enforce")
    first = await selector.select(req)
    await promote(db, first)
    truncated = await selector.select(req)
    assert truncated["usage"]["evidence_complete"] is False
    assert truncated["jev_used_for_omission"] is False
    assert truncated["fallback_reason"] == "evidence_incomplete"
    assert truncated["final_modules"] == truncated["fallback_modules"]
    config.excerpt_lines = 40
    complete = await selector.select(req)
    assert complete["snapshot_fingerprint"] == first["snapshot_fingerprint"]
    assert complete["cache_key"] != first["cache_key"]
    assert len(transport.calls) == 2
    assert complete["jev_used_for_omission"] is True


async def test_explicit_module_survives_promoted_omission(db, repo):
    modify(repo)
    selector = service(db, transport=FakeTransport(response(all_unaffected=True)))
    req = request(repo, mode="enforce", targets=("tests/sub/test_b.py",))
    first = await selector.select(req)
    await promote(db, first)
    record = await selector.select(req)
    assert record["jev_used_for_omission"] is True
    assert record["final_modules"] == ["tests/sub/test_b.py", "tests/test_a.py"]
    assert "explicit_target" in record["reasons"]["tests/sub/test_b.py"]


@pytest.mark.parametrize("missing", ["binary", "oversize"])
async def test_missing_content_evidence_blocks_promoted_omission(db, repo, monkeypatch, missing):
    if missing == "binary":
        (repo / "src/pkg/a.py").write_bytes(b"\0binary data")
    else:
        modify(repo)
        monkeypatch.setattr("src.test_selection.snapshot.MAX_HASH_BYTES", 8)
    selector = service(db, transport=FakeTransport(response(all_unaffected=True)))
    req = request(repo, mode="enforce")
    first = await selector.select(req)
    await promote(db, first)
    record = await selector.select(req)
    assert record["snapshot_complete"] is True
    assert record["usage"]["evidence_complete"] is False
    assert record["fallback_reason"] == "evidence_incomplete"
    assert record["jev_used_for_omission"] is False
    assert record["final_modules"] == record["fallback_modules"]


async def test_config_gates_are_read_per_call_before_any_persistence(db, repo):
    modify(repo)
    config = SelectionConfig(enabled=False)
    transport = FakeTransport()
    selector = service(db, config=config, transport=transport)
    with pytest.raises(ValueError, match="^disabled$"):
        await selector.select(request(repo))
    config.enabled = True
    with pytest.raises(ValueError, match="^enforce_disabled$"):
        await selector.select(request(repo, mode="enforce"))
    assert await db.list_test_selections(project_id="p") == []
    record = await selector.select(request(repo))
    assert record["jev_status"] == "disabled"
    assert transport.calls == []


async def test_static_deadline_cancels_engine_and_skips_network(db, repo):
    modify(repo)

    class HangingStatic:
        cancelled = False

        async def impacted(self, snapshot, *, catalogue):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

    static = HangingStatic()
    transport = FakeTransport()
    config = SelectionConfig(enabled=True, jev_enabled=True, static_timeout_seconds=0.01)
    record = await service(db, static=static, config=config, transport=transport).select(
        request(repo)
    )
    assert static.cancelled and transport.calls == []
    assert record["full_required"] is True
    assert record["fallback_reason"] == "static_timeout"


async def test_packing_budget_is_checked_before_cached_answers(db, repo):
    modify(repo)
    config = SelectionConfig(enabled=True, jev_enabled=True)
    transport = FakeTransport(response())
    selector = service(db, config=config, transport=transport)
    await selector.select(request(repo))
    config.max_total_tokens = 1
    record = await selector.select(request(repo))
    assert record["jev_status"] == "over_budget"
    assert record["final_modules"] == record["fallback_modules"]
    assert len(transport.calls) == 1
    assert "jev_cached" not in record["usage"]["reasons"]


async def test_unknown_base_falls_back_without_network(db, repo):
    transport = FakeTransport()
    record = await service(db, transport=transport).select(request(repo, base_ref="missing-base"))
    assert record["snapshot_complete"] is False
    assert record["incomplete_reason"] == "unknown_base"
    assert record["full_required"] is True
    assert len(record["final_modules"]) == 4 and transport.calls == []


def test_offline_is_plan_only_without_network_or_database(repo, monkeypatch):
    modify(repo)
    monkeypatch.setattr("src.test_selection.service.shutil.which", lambda executable: None)
    record = select_offline(str(repo), base_ref="origin/main", targets=())
    assert record["recorded"] is False and "id" not in record
    assert record["mode"] == "plan_only" and record["jev_status"] == "disabled"
    assert record["mandatory_modules"] == sorted(record["final_modules"])


def test_config_defaults_validation_roundtrip_and_hot_reload(tmp_path):
    config = SelectionConfig()
    assert config.validate() == []
    assert not any((config.enabled, config.jev_enabled, config.enforce_enabled))
    assert SelectionConfig(api_key_env="sk-live.abc").validate()[0].field == "api_key_env"
    values = asdict(
        SelectionConfig(
            enabled=True,
            jev_enabled=True,
            enforce_enabled=True,
            api_key_env="CUSTOM_TYPESAFE_KEY",
            base_url="https://test.invalid",
            default_base_ref="origin/develop",
            max_requests=7,
        )
    )
    path = tmp_path / "config.yaml"
    raw = {
        "database": {"url": "postgresql+asyncpg://test:test@localhost/test"},
        "discord": {"bot_token": "t", "guild_id": "1"},
        "test_selection": values,
    }
    path.write_text(yaml.safe_dump(raw))
    loaded = load_config(str(path))
    assert asdict(loaded.test_selection) == values
    raw["test_selection"]["enabled"] = False
    path.write_text(yaml.safe_dump(raw))
    assert loaded.reload_non_critical().test_selection.enabled is False
    assert loaded.test_selection.enabled is True
    assert any(
        e.section == "test_selection"
        for e in AppConfig(test_selection=SelectionConfig(max_requests=0)).validate()
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("rpc_deadline_seconds", float("nan")),
        ("static_timeout_seconds", float("inf")),
        ("max_requests", 0),
        ("cache_entries", -1),
        ("excerpt_lines", True),
        ("request_concurrency", 1.5),
        ("model", " "),
        ("api_key_env", "a"),
        ("api_key_env", "A" * 65),
    ],
)
def test_config_rejects_invalid_fields(field, value):
    assert any(e.field == field for e in SelectionConfig(**{field: value}).validate())
