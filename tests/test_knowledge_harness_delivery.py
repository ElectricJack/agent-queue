"""K09 — thin harness delivery/resume adapters and the integrated fixture runner.

Three layers, all exercised here:

* the pure transport adapters (``src/knowledge/delivery.py``), which classify a
  launch, suppress a duplicate and say nothing about comprehension;
* the real delivery ledger (``src.knowledge.context``) reached through those
  adapters, including a retried acknowledgment, a compaction/resume source and a
  provider switch that lowers the budget;
* the provider-neutral evaluation runner replayed against the real service, for
  every harness label and for a local model with neither hook nor prompt.

Design refs: docs/specs/knowledge-context.md (K08 delivery ledger, K09
adapters), plan §9 delivery seams and §13 evaluation fixtures.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar

import pytest
from sqlalchemy import func, select, update

from src.commands.principal import TRUSTED_LOCAL
from src.database.tables import (
    knowledge_citations,
    knowledge_context_deliveries,
    sessions,
    tasks,
)
from src.knowledge.budget import ContextBudget
from src.knowledge.context import context_enabled
from src.knowledge.delivery import (
    DELIVERED,
    FAILED,
    HOOK_ENVELOPE,
    POINTER_BEGIN,
    STARTUP_GUIDANCE,
    STARTUP_GUIDANCE_FILE,
    STARTUP_PROMPT,
    UNKNOWN,
    KnowledgeSelection,
    acknowledge,
    memory_pointer,
    plan,
    startup_guidance,
    transport_for,
    transport_key,
)
from src.records.models import RecordError
from tests.knowledge_fixture_adapter import (
    ContextBundleFixtureAdapter,
    FixtureAdapterError,
    LocalModelFixtureAdapter,
)
from tests.test_knowledge_context import context_setup as _context_setup
from tests.test_knowledge_context import prepare as prepare_bundle

context_setup = _context_setup

INTEGRATED = sorted(
    (Path(__file__).parent / "fixtures/knowledge/integrated").glob("*.json")
)
PAYLOAD = "## Knowledge context\n\nRecord r revision v sha256:abc\n> Retained evidence.\n"


# ---------------------------------------------------------------------------
# Transport classification and suppression (pure)
# ---------------------------------------------------------------------------


class TestTransportMatrix:
    def test_provisioned_hook_is_the_only_structured_transport(self):
        assert transport_for("claude", supports_hooks=True) == HOOK_ENVELOPE
        assert transport_for("Codex", supports_hooks=True) == HOOK_ENVELOPE

    def test_withheld_hook_trust_leaves_the_startup_prompt(self):
        # A hook whose trust was withheld is not a hook: the payload must still
        # ride the prompt rather than be dropped.
        assert transport_for("claude", supports_hooks=False) == STARTUP_PROMPT
        assert transport_for("codex", supports_hooks=False) == STARTUP_PROMPT

    def test_harness_without_a_hook_contract_never_claims_the_envelope(self):
        assert transport_for("opencode", supports_hooks=True) == STARTUP_PROMPT
        assert transport_for("gemini", supports_hooks=True) == STARTUP_PROMPT

    def test_no_prompt_channel_falls_back_to_written_guidance(self):
        assert transport_for("gemini", prompt_mode="none") == STARTUP_GUIDANCE
        assert transport_for("", prompt_mode="none") == STARTUP_GUIDANCE

    @pytest.mark.parametrize("harness", ["claude", "codex", "opencode", "gemini", "", "local"])
    def test_identical_payload_for_every_harness_label(self, harness):
        delivery = plan(
            PAYLOAD, bundle_id="b-1", harness=harness,
            supports_hooks=harness in {"claude", "codex"},
        )
        assert delivery.payload == PAYLOAD
        assert delivery.rendered in {PAYLOAD, _envelope(PAYLOAD, harness)}
        assert not delivery.suppressed


def _envelope(body: str, harness: str) -> str:
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": body,
        }
    })


class TestDuplicateSuppression:
    ENV: ClassVar = {"AQ_STARTUP_PROMPT_DELIVERED": "1"}

    def test_startup_prompt_already_delivered_suppresses_the_hook(self):
        delivery = plan(
            PAYLOAD, bundle_id="b-1", harness="claude", supports_hooks=True, env=self.ENV,
        )
        assert delivery.suppressed and delivery.rendered == ""

    @pytest.mark.parametrize("source", ["compact", "resume"])
    def test_compaction_and_resume_always_deliver(self, source):
        delivery = plan(
            PAYLOAD, bundle_id="b-1", harness="claude", supports_hooks=True,
            env=self.ENV, source=source,
        )
        assert not delivery.suppressed
        assert delivery.rendered == _envelope(PAYLOAD, "claude")
        assert source in delivery.transport_key

    def test_hook_without_a_delivered_startup_prompt_is_not_suppressed(self):
        assert not plan(
            PAYLOAD, bundle_id="b-1", harness="claude", supports_hooks=True, env={},
        ).suppressed

    def test_startup_prompt_transport_is_never_suppressed(self):
        assert not plan(
            PAYLOAD, bundle_id="b-1", harness="opencode", env=self.ENV,
        ).suppressed


class TestAcknowledgmentAndKeys:
    @pytest.mark.parametrize(
        "observed,state", [(True, DELIVERED), (False, FAILED), (None, UNKNOWN)]
    )
    def test_only_an_observed_write_is_delivered(self, observed, state):
        delivery = plan(PAYLOAD, bundle_id="b-1", harness="opencode")
        assert acknowledge(delivery, observed=observed).state == state

    def test_a_suppressed_delivery_is_never_acknowledged(self):
        delivery = plan(
            PAYLOAD, bundle_id="b-1", harness="claude", supports_hooks=True,
            env={"AQ_STARTUP_PROMPT_DELIVERED": "1"},
        )
        with pytest.raises(ValueError):
            acknowledge(delivery, observed=True)

    def test_the_same_attempt_always_mints_the_same_key(self):
        first = transport_key(
            transport=STARTUP_PROMPT, bundle_id="b-1", session_id="s-1", claim_epoch=1,
        )
        retry = transport_key(
            transport=STARTUP_PROMPT, bundle_id="b-1", session_id="s-1", claim_epoch=1,
        )
        assert first == retry
        assert first.startswith(f"{STARTUP_PROMPT}:b-1:")

    def test_a_recycled_slot_cannot_reuse_the_previous_task_key(self):
        previous = transport_key(
            transport=STARTUP_PROMPT, bundle_id="b-1", session_id="s-1", claim_epoch=1,
        )
        current = transport_key(
            transport=STARTUP_PROMPT, bundle_id="b-1", session_id="s-1", claim_epoch=2,
        )
        assert previous != current

    def test_long_identities_stay_inside_the_transport_key_column(self):
        key = transport_key(
            transport=HOOK_ENVELOPE, bundle_id="b-" * 80, session_id="s-" * 80,
            claim_epoch=12,
        )
        assert 1 <= len(key) <= 128


class TestGuidanceAndPointerBlock:
    def test_guidance_carries_the_payload_and_names_the_commands(self):
        guidance = startup_guidance(PAYLOAD, harness="gemini", bundle_id="b-1")
        assert PAYLOAD.strip() in guidance
        assert "aq prime" in guidance
        assert "not session instructions or approval" in guidance
        assert "b-1" in guidance

    def test_pointer_block_is_opt_in_and_preserves_operator_bytes(self):
        existing = "# My own memory\n\nnotes\n"
        assert memory_pointer(existing) == existing
        with_block = memory_pointer(existing, enabled=True)
        assert with_block.startswith("# My own memory\n\nnotes\n")
        assert POINTER_BEGIN in with_block
        # Re-applying is idempotent and still preserves the operator's bytes.
        assert memory_pointer(with_block, enabled=True) == with_block

    def test_pointer_block_replaces_only_its_own_markers(self):
        stamped = memory_pointer("user bytes\n", enabled=True)
        replaced = memory_pointer(stamped.replace("Managed by Agent Queue", "OLD"), enabled=True)
        assert "OLD" not in replaced
        assert replaced.startswith("user bytes\n")


# ---------------------------------------------------------------------------
# The real ledger, reached through the adapters
# ---------------------------------------------------------------------------


async def _deliver(service, principal, bundle, key, *, state=DELIVERED, claim_epoch=1):
    return await service.observe_delivery(
        bundle_id=bundle.bundle_id, principal=principal, claim_epoch=claim_epoch,
        transport=STARTUP_PROMPT, transport_key=key, state=state,
        rendered_sha256=bundle.content_sha256,
    )


async def _citations(db):
    async with db.immediate() as conn:
        return (await conn.execute(select(knowledge_citations))).mappings().all()


class TestDeliveryLedger:
    async def test_observed_delivery_pins_the_exact_revision_once(self, context_setup):
        db, _, service, worker, _, created = context_setup
        bundle = await prepare_bundle(service, worker)
        delivery = plan(bundle.to_markdown(), bundle_id=bundle.bundle_id, harness="opencode",
                        session_id=worker.session_id, claim_epoch=1)
        await _deliver(service, worker, bundle, delivery.transport_key)
        # A retried acknowledgment is the same observation, not a second one.
        await _deliver(service, worker, bundle, delivery.transport_key)
        assert acknowledge(delivery, observed=True).state == DELIVERED
        rows = await _citations(db)
        assert [str(row["revision_id"]) for row in rows] == [created["revision_id"]]
        assert rows[0]["kind"] == "injected" and rows[0]["claim_epoch"] == 1

    async def test_unknown_acknowledgment_cites_nothing_until_a_retry(self, context_setup):
        db, _, service, worker, _, _ = context_setup
        bundle = await prepare_bundle(service, worker)
        key = transport_key(
            transport=STARTUP_PROMPT, bundle_id=bundle.bundle_id,
            session_id=worker.session_id, claim_epoch=1,
        )
        assert (await _deliver(service, worker, bundle, key, state=UNKNOWN))["state"] == UNKNOWN
        assert await _citations(db) == []
        assert (await _deliver(service, worker, bundle, key))["state"] == DELIVERED
        assert len(await _citations(db)) == 1
        async with db.immediate() as conn:
            assert await conn.scalar(
                select(func.count()).select_from(knowledge_context_deliveries)
            ) == 1

    async def test_compaction_and_resume_reauthorize_under_the_current_claim(self, context_setup):
        db, _, service, worker, _, _ = context_setup
        first = await prepare_bundle(service, worker)
        resumed = await prepare_bundle(service, worker, refresh=True)
        keys = {
            source: transport_key(
                transport=HOOK_ENVELOPE, bundle_id=bundle.bundle_id,
                session_id=worker.session_id, claim_epoch=1, source=source,
            )
            for source, bundle in (("compact", first), ("resume", resumed))
        }
        assert len(set(keys.values())) == 2
        for key in keys.values():
            assert (await _deliver(service, worker, first, key))["success"]
            # The key names one bundle: a compaction receipt cannot be reused to
            # deliver the refreshed bundle, so the source has to key its own.
            with pytest.raises(RecordError, match="record.idempotency_conflict"):
                await _deliver(service, worker, resumed, key)
        # Each source delivered the same revision under its own key, and the
        # citation is recorded once for the one execution owner.
        assert len(await _citations(db)) == 1

    async def test_a_provider_switch_lowers_the_budget_and_the_selection(self, context_setup):
        _, _, service, worker, _, _ = context_setup
        full = await prepare_bundle(service, worker)
        assert full.items
        switched = await prepare_bundle(
            service, worker, refresh=True,
            budget=ContextBudget(input_tokens=6000, knowledge_tokens=64, knowledge_bytes=64),
        )
        assert not switched.items
        assert {omission["reason"] for omission in switched.omissions} == {"knowledge_budget"}
        # The pinned evidence stays selectable once the window allows it again.
        assert (await prepare_bundle(service, worker)).items == full.items

    async def test_a_recycled_slot_cannot_deliver_the_previous_task_payload(self, context_setup):
        db, _, service, worker, _, _ = context_setup
        bundle = await prepare_bundle(service, worker)
        async with db.immediate() as conn:
            await conn.execute(
                update(tasks).where(tasks.c.id == worker.task_id).values(claim_epoch=2)
            )
            await conn.execute(
                update(sessions).where(sessions.c.id == worker.session_id)
                .values(instance_token="next-task-instance")
            )
        with pytest.raises(RecordError):
            await _deliver(service, worker, bundle, f"{STARTUP_PROMPT}:stale")
        assert await _citations(db) == []

    async def test_a_suppressed_launch_writes_no_receipt(self, context_setup):
        db, _, service, worker, _, _ = context_setup
        bundle = await prepare_bundle(service, worker)
        delivery = plan(
            bundle.to_markdown(), bundle_id=bundle.bundle_id, harness="claude",
            supports_hooks=True, env={"AQ_STARTUP_PROMPT_DELIVERED": "1"},
        )
        assert delivery.suppressed
        with pytest.raises(ValueError):
            acknowledge(delivery, observed=None)
        async with db.immediate() as conn:
            assert await conn.scalar(
                select(func.count()).select_from(knowledge_context_deliveries)
            ) == 0

    async def test_delivery_stays_off_while_the_feature_is_disabled(self, context_setup):
        _, config, service, worker, _, _ = context_setup
        config.knowledge.context.enabled = False
        assert not context_enabled(config)
        with pytest.raises(RecordError, match="context.disabled"):
            await prepare_bundle(service, worker)


# ---------------------------------------------------------------------------
# Launch wiring: the spec records what it provisioned
# ---------------------------------------------------------------------------


@dataclass
class _Task:
    id: str = "task-1"
    project_id: str = "proj-1"
    intelligence_class: str | None = None


@dataclass
class _Profile:
    id: str = "worker-claude"
    default_class: str = ""
    effort: str = ""
    harness: str = "claude"
    permission_mode: str = ""
    codex_full_auto: bool = False
    claude_dangerously_skip_permissions: bool = False


class _McpCfg:
    host = "127.0.0.1"
    port = 8081


class _Cfg:
    mcp_server = _McpCfg()
    security = None
    data_dir = "/tmp/aq"


def _harness(**overrides):
    from src.sessions.harness_parser import Harness, ResumeSpec

    defaults = {
        "id": "gemini", "name": "Gemini", "command": "gemini", "prompt_mode": "arg",
        "permission_flag": "--yolo", "model_flag": "-m",
        "session_id_flag": "--session-id", "resume": ResumeSpec(style="none"),
        "process_names": ("gemini",), "max_argv_prompt_bytes": 1024,
        "supports_hooks": False,
    }
    return Harness(**{**defaults, **overrides})


def _spec(harness, *, knowledge=None):
    from src.sessions.spec import SessionSpecBuilder

    return SessionSpecBuilder(_Cfg()).build_task_spec(
        task=_Task(), profile=_Profile(), harness=harness, work_dir="/wd",
        session_id="sess-abc", instance_token="tok-1", epoch="1", knowledge=knowledge,
    )


class TestLaunchWiring:
    def test_a_launch_without_a_bundle_records_no_transport(self):
        assert _spec(_harness()).knowledge_transport is None

    def test_a_hook_harness_records_the_envelope_transport(self):
        selection = KnowledgeSelection(markdown=PAYLOAD, bundle_id="b-1")
        spec = _spec(
            _harness(
                id="claude", supports_hooks=True, settings_flag="--settings",
                hook_files=((".aq/hooks/claude.json", "hooks/claude.json"),),
            ),
            knowledge=selection,
        )
        assert spec.hooks_provisioned is True
        assert spec.knowledge_transport == HOOK_ENVELOPE

    def test_a_no_hook_harness_records_the_startup_prompt(self):
        selection = KnowledgeSelection(markdown=PAYLOAD, bundle_id="b-1")
        assert _spec(_harness(), knowledge=selection).knowledge_transport == STARTUP_PROMPT

    def test_no_hook_and_no_prompt_channel_writes_guidance_into_the_workspace(self):
        selection = KnowledgeSelection(markdown=PAYLOAD, bundle_id="b-1")
        spec = _spec(_harness(prompt_mode="none"), knowledge=selection)
        assert spec.knowledge_transport == STARTUP_GUIDANCE
        assert spec.prompt is None
        written = dict(spec.files)
        assert set(written) == {STARTUP_GUIDANCE_FILE}
        assert PAYLOAD.strip() in written[STARTUP_GUIDANCE_FILE]

    def test_the_guidance_file_stays_inside_the_workspace(self):
        from src.sessions.subprocess import _write_spec_files

        selection = KnowledgeSelection(markdown=PAYLOAD, bundle_id="b-1")
        spec = replace(
            _spec(_harness(prompt_mode="none"), knowledge=selection),
            work_dir="/tmp/aq-k09-guidance",
            files=((STARTUP_GUIDANCE_FILE, "guidance\n"), ("../escape.md", "nope\n")),
        )
        _write_spec_files(spec)
        root = Path(spec.work_dir)
        assert (root / STARTUP_GUIDANCE_FILE).read_text() == "guidance\n"
        assert not (root.parent / "escape.md").exists()
        import shutil

        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# The provider-neutral runner, against the real service
# ---------------------------------------------------------------------------


def _manifest(name):
    path = Path(__file__).parent / f"fixtures/knowledge/integrated/{name}.json"
    return json.loads(path.read_text())


@pytest.fixture
async def integrated_db(reuse_database):
    return await reuse_database("knowledge-harness")


@pytest.fixture(scope="module")
def runner():
    """The provider-neutral runner itself, loaded the way the README documents."""
    import runpy
    import sys

    script = Path(__file__).resolve().parents[1] / "scripts/evaluate-knowledge.py"
    if str(Path(__file__).resolve().parents[1]) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    return runpy.run_path(str(script))


@pytest.mark.parametrize("name", [path.stem for path in INTEGRATED])
@pytest.mark.parametrize(
    "adapter_class", [ContextBundleFixtureAdapter, LocalModelFixtureAdapter]
)
async def test_integrated_fixture_passes_the_runner_oracle(integrated_db, runner, name,
                                                           adapter_class):
    """One real-service pass per harness label, judged by the independent oracle."""
    manifest = _manifest(name)
    adapter = await adapter_class.run(integrated_db, manifest)
    report = runner["evaluate_manifest"](manifest, adapter)
    assert report["status"] == "pass", report
    assert {check["harness"] for check in report["checks"]} == {
        "claude", "codex", "opencode", "local",
    }
    assert all(check["status"] == "pass" for check in report["checks"])
    assert all(check["metrics"] for check in report["checks"] if check["metrics"] or True)


def test_integrated_manifests_are_present_and_sealed():
    assert {path.stem for path in INTEGRATED} == {
        "duplicate-acknowledgment", "hook-and-startup-parity", "required-content-over-budget",
    }
    for path in INTEGRATED:
        manifest = json.loads(path.read_text())
        assert manifest["source_class"] == "synthetic" and manifest["synthetic"] is True
        assert all(record.get("revision_sha256") for record in manifest["records"])


async def test_the_adapter_never_reads_the_hidden_oracle(integrated_db):
    """An oracle that claims nothing was allowed still observes a full selection.

    The adapter is handed the manifest without ``expected``; seeding, selection
    and citations come from the sealed ``records`` and the service, so a hollow
    oracle cannot make it select less.
    """
    manifest = _manifest("hook-and-startup-parity")
    manifest["fixture_id"] = "parity-behind-a-hollow-oracle"
    manifest["expected"] = {
        "allowed_identities": [], "omitted_reasons": {}, "forbidden_records": {},
        "duplicate_delivery": {"attempts": []}, "required_budget_diagnostic": True,
    }
    adapter = await ContextBundleFixtureAdapter.run(integrated_db, manifest)
    inputs = {
        key: value for key, value in manifest.items() if key not in ("expected", "input_hashes")
    }
    observed = adapter.observe(inputs, harness="claude", role="worker")
    assert [item["record_id"] for item in observed["selected"]] == [
        "rec-integrated-evidence", "rec-integrated-procedure",
    ]
    assert [citation["record_id"] for citation in observed["citations"]] == [
        "rec-integrated-evidence", "rec-integrated-procedure",
    ]


@pytest.mark.parametrize(
    "adapter_class,expected",
    [
        (ContextBundleFixtureAdapter, {"claude": HOOK_ENVELOPE, "codex": HOOK_ENVELOPE,
                                       "opencode": STARTUP_PROMPT, "local": STARTUP_PROMPT}),
        (LocalModelFixtureAdapter, {"claude": STARTUP_GUIDANCE, "codex": STARTUP_GUIDANCE,
                                    "opencode": STARTUP_GUIDANCE, "local": STARTUP_GUIDANCE}),
    ],
)
async def test_every_harness_label_reaches_the_same_payload(integrated_db, adapter_class,
                                                             expected):
    manifest = _manifest("hook-and-startup-parity")
    adapter = await adapter_class.run(integrated_db, manifest)
    assert adapter.transports == {
        ("hook-and-startup-parity", harness): transport
        for harness, transport in expected.items()
    }
    observed = {
        harness: adapter.observe(
            {key: value for key, value in manifest.items() if key not in ("expected", "input_hashes")},
            harness=harness, role="worker",
        )
        for harness in manifest["labels"]["harnesses"]
    }
    payloads = {json.dumps(value["rendered"], sort_keys=True) for value in observed.values()}
    assert len(payloads) == 1
    assert all(value["citations"] for value in observed.values())
    assert {citation["record_id"] for citation in observed["claude"]["citations"]} == {
        "rec-integrated-evidence", "rec-integrated-procedure",
    }


async def test_retried_acknowledgment_leaves_one_receipt_and_one_citation(integrated_db):
    manifest = _manifest("duplicate-acknowledgment")
    adapter = await ContextBundleFixtureAdapter.run(integrated_db, manifest)
    observed = adapter.observe(
        {key: value for key, value in manifest.items() if key not in ("expected", "input_hashes")},
        harness="opencode", role="worker",
    )
    assert [receipt["new"] for receipt in observed["deliveries"]] == [True, False]
    assert [receipt["final_state"] for receipt in observed["deliveries"]] == [
        UNKNOWN, DELIVERED,
    ]
    assert len(observed["citations"]) == 1
    async with integrated_db.immediate() as conn:
        assert await conn.scalar(
            select(func.count()).select_from(knowledge_context_deliveries)
        ) == 1


async def test_required_over_budget_delivers_nothing_and_says_so(integrated_db):
    manifest = _manifest("required-content-over-budget")
    adapter = await ContextBundleFixtureAdapter.run(integrated_db, manifest)
    observed = adapter.observe(
        {key: value for key, value in manifest.items() if key not in ("expected", "input_hashes")},
        harness="claude", role="worker",
    )
    assert observed["selected"] == [] and observed["citations"] == []
    assert observed["deliveries"] == []
    assert observed["diagnostics"] == ["context.required_over_budget"]
    assert observed["rendered"] == ""


async def test_a_substituted_revision_fails_before_the_oracle_runs(integrated_db):
    manifest = _manifest("hook-and-startup-parity")
    manifest["records"][0]["revision_sha256"] = "0" * 64
    with pytest.raises(FixtureAdapterError, match="sealed revision hash"):
        await ContextBundleFixtureAdapter.run(integrated_db, manifest)


async def test_the_local_model_adapter_reports_a_contract_not_a_quality_claim(integrated_db):
    manifest = _manifest("hook-and-startup-parity")
    adapter = await LocalModelFixtureAdapter.run(integrated_db, manifest)
    assert adapter.name == "local_model_contract"
    assert adapter.transports[("hook-and-startup-parity", "local")] == STARTUP_GUIDANCE
    observed = adapter.observe(
        {key: value for key, value in manifest.items() if key not in ("expected", "input_hashes")},
        harness="local", role="worker",
    )
    # Contract compatibility only: the observation carries no model output.
    assert set(observed) == {
        "selected", "omissions", "rendered", "rendered_sha256", "owner", "citations",
        "deliveries", "usage", "diagnostics", "metadata",
    }


async def test_a_supervisor_owner_delivers_under_its_own_session(integrated_db):
    manifest = copy.deepcopy(_manifest("hook-and-startup-parity"))
    manifest["fixture_id"] = "supervisor-owned-delivery"
    rules = manifest["delivery_rules"]
    rules.update(
        role="supervisor", owner_kind="supervisor_session",
        owner_id="session-supervisor-owned-delivery",
        bundle_id="bundle-supervisor-owned-delivery",
    )
    for attempt in manifest["expected"]["duplicate_delivery"]["attempts"]:
        attempt.update(
            bundle_id=rules["bundle_id"],
            transport_key=f"{rules['bundle_id']}:hook_envelope",
        )
    for event in rules["events"]:
        event.update(
            bundle_id=rules["bundle_id"],
            transport_key=f"{rules['bundle_id']}:hook_envelope",
        )
    import runpy
    import sys

    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    runpy.run_path(str(root / "scripts/evaluate-knowledge.py"))["seal_manifest"](manifest)
    adapter = await ContextBundleFixtureAdapter.run(integrated_db, manifest)
    observed = adapter.observe(
        {key: value for key, value in manifest.items() if key not in ("expected", "input_hashes")},
        harness="claude", role="supervisor",
    )
    assert observed["owner"] == {
        "owner_kind": "supervisor_session",
        "owner_id": "session-supervisor-owned-delivery",
        "claim_epoch": 1,
    }
    async with integrated_db.immediate() as conn:
        rows = (await conn.execute(select(knowledge_citations))).mappings().all()
    assert {row["owner_kind"] for row in rows} == {"supervisor_session"}
    assert len({row["supervisor_session_id"] for row in rows}) == 1


def test_fixture_seeding_never_invents_verification_provenance():
    from tests.knowledge_fixture_adapter import _seed_snapshot

    record = dict(_manifest("hook-and-startup-parity")["records"][0])
    record["verification"] = "verified"
    with pytest.raises(FixtureAdapterError, match="unverified revisions only"):
        _seed_snapshot(record)


def test_trusted_local_never_becomes_an_execution_owner():
    """Seeding uses the operator principal; delivery never does."""
    assert TRUSTED_LOCAL.policy.allows_aq_command("knowledge_context_deliver") is False