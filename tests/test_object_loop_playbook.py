"""AQ-3: real command traces, replayed through V2's write-free dry runner."""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import insert, select, update

from src.commands.contracts.registry import CONTRACTS, ContractRegistry
from src.commands.principal import (
    TRUSTED_LOCAL,
    ExecutionPrincipal,
    PrincipalKind,
    principal_context,
)
from src.database.tables import doc_review_revisions, doc_reviews, object_loops, tasks
from src.integration.development import _publishable_object_task
from src.models import Project, TaskStatus
from src.object_loop.inputs import SCORE_PREFIX
from src.playbooks.definition import load_definition_json
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors.base import EngineServices
from src.playbooks.required import DEFAULT_SYSTEM_PLAYBOOK_IDS, REQUIRED_SYSTEM_PLAYBOOK_IDS
from src.profiles.capabilities import CapabilityPolicy
from src.task_graph.formulas import FormulaRegistry, load_from_vault
from src.vault import ensure_default_formulas
from tests.playbook_v2_engine_helpers import (
    InMemoryArtifactStore,
    RecordingRunRepository,
    artifact_ref_for,
)
from tests.test_object_loop import B, C, H, receipt, start_args, variant

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "tests/fixtures/playbooks/v2/object-loop"
PROJECT = "matter-engine-cpp"
COMMANDS = (
    "object_loop_inputs", "object_loop_start", "object_loop_reconcile",
    "object_score_record", "object_checkpoint_read",
)


def artifact():
    return load_definition_json((BUNDLE / "artifact.json").read_text())


async def review(db, review_id="brief-review", kind="other", revision=1, digest=C,
                 *, decider="user"):
    gate, _ = await db.create_gate(PROJECT, "review", review_id, await_id=review_id)
    await db.resolve_gate(gate, resolved_by="human", resolution="approved")
    async with db.immediate() as conn:
        await conn.execute(insert(doc_reviews).values(
            id=review_id, project_id=PROJECT, kind=kind, title=review_id,
            vault_path=f"projects/{PROJECT}/{review_id}.md", current_revision=revision,
            state="approved", gate_id=gate, decider=decider, decided_by="human",
            decided_at=1, created_at=1, updated_at=1,
        ))
        await conn.execute(insert(doc_review_revisions).values(
            review_id=review_id, revision=revision, content="fixture", content_sha256=digest,
            submitted_by="agent", submitted_at=1,
        ))
    return gate


@pytest.fixture
async def setup(command_handler_factory, tmp_path, monkeypatch):
    handler = await command_handler_factory()
    from src.commands.contracts import builtin

    monkeypatch.setattr(builtin, "_handler_provider", lambda: handler)
    await handler.db.create_project(Project(id=PROJECT, name="Matter"))
    await review(handler.db)
    await review(handler.db, "rev-amber-zenith", "spec", 2, H)
    ensure_default_formulas(str(tmp_path))
    registry = FormulaRegistry()
    assert load_from_vault(registry, str(tmp_path / "vault")) == []
    handler.orchestrator.formula_registry = registry
    handler.config.data_dir = str(tmp_path)
    yield handler
    await handler.db.close()


def variables(reference_kind="calibrated"):
    request = start_args()
    request["reference_kind"] = reference_kind
    request.pop("project_id")
    request.pop("epic_task_id")
    request["policy_sha256"] = artifact_ref_for(artifact()).artifact_sha256.removeprefix("sha256:")
    request["limits"] = {"usd": 30, "calls": 30, "bakes": 30, "active_seconds": 300}
    return {"object_id": "rock", "proposal_sha256": H, "start": json.dumps(request)}


async def cook(handler, *, reference_kind="calibrated"):
    result = await handler._cmd_formula_cook({
        "name": "object", "project_id": PROJECT, "vars": variables(reference_kind),
    })
    assert result["success"], result
    return result


async def sweep(handler, *, rule="recover"):
    """Execute real handlers on disposable PostgreSQL, then replay read-only.

    Every preview is a recording of this exact live command's arguments and
    result. It cannot hit the handler/database, and a differing traversal fails.
    """
    definition = artifact()
    ref = artifact_ref_for(definition)
    store = InMemoryArtifactStore()
    store.put(definition)
    live = ContractRegistry()
    recordings = []
    for name in COMMANDS:
        registration = CONTRACTS.require(name)

        async def invoke(args, principal, name=name, registration=registration):
            result = await registration.invoke(args, principal)
            recordings.append((name, args.model_dump(), copy.deepcopy(result)))
            return result

        live.register(replace(registration, invoke=invoke))
    engine = PlaybookEngine(
        services=EngineServices(contracts=live, artifact_store=store, clock=time.time),
        runs=RecordingRunRepository(),
    )
    event_type = next(r.trigger.event_type for r in definition.rules if r.id == rule)
    event = {"event_type": event_type, "project_id": PROJECT, "event_id": uuid.uuid4().hex}
    result = await engine.run_rule(ref, rule, event, TRUSTED_LOCAL)
    assert result.outcome == "completed", (result, recordings)

    remaining = list(recordings)
    previews = ContractRegistry()
    for name in COMMANDS:
        registration = CONTRACTS.require(name)

        async def preview(args, _principal, name=name):
            expected_name, expected_args, recorded = remaining.pop(0)
            assert (name, args.model_dump()) == (expected_name, expected_args)
            return recorded

        contract = registration.contract.model_copy(update={
            "execution": registration.contract.execution.model_copy(update={"supports_preview": True})
        })
        previews.register(replace(registration, contract=contract, preview=preview))
    dry_engine = PlaybookEngine(
        services=EngineServices(contracts=previews, artifact_store=store, clock=time.time),
        runs=RecordingRunRepository(),
    )
    tree = await dry_engine.dry_run(ref, event, TRUSTED_LOCAL)
    assert not tree.truncated and len(tree.paths) == 1
    assert tree.paths[0].completed, tree
    assert not remaining, json.dumps({
        "remaining": [name for name, _, _ in remaining],
        "nodes": [(n.step_id, n.target, n.outcome) for n in tree.paths[0].nodes],
        "inputs": recordings[0][2].value.model_dump(),
        "artifact": definition.artifact_sha256(),
    }, default=str)
    if trace_dir := os.getenv("AQ_OBJECT_LOOP_TRACE_DIR"):
        destination = Path(trace_dir)
        destination.mkdir(parents=True, exist_ok=True)
        reviews = await handler.db.list_reviews(project_id=PROJECT)
        (destination / f"{uuid.uuid4().hex}.json").write_text(json.dumps({
            "test": os.environ.get("PYTEST_CURRENT_TEST", "").split(" (", 1)[0],
            "rule": rule, "artifact_sha256": ref.artifact_sha256,
            "live_outcome": result.outcome, "dry_completed": tree.paths[0].completed,
            "commands": [{"name": name, "outcome": result.outcome}
                         for name, _, result in recordings],
            "dry_nodes": [{"step": n.step_id, "outcome": n.outcome, "target": n.target}
                          for n in tree.paths[0].nodes],
            "review_audiences": sorted(
                [{"decider": r["decider"], "kind": r["kind"], "title": r["title"]}
                 for r in reviews], key=lambda r: (r["decider"], r["kind"], r["title"]),
            ),
        }, indent=2) + "\n")
    return recordings, tree


async def state(handler):
    result = await handler._cmd_object_checkpoint_read({"project_id": PROJECT, "object_id": "rock"})
    assert result["success"], result
    return result


async def complete_wave(handler):
    current = await state(handler)
    for member in current["state"]["wave"]:
        await handler.db.update_task(member["task_id"], status=TaskStatus.COMPLETED)
    await sweep(handler, rule="on-completion")
    return await state(handler)


async def score_packet(handler, current, *, loss=0.1, invalid=False, action="continue",
                       next_variants=None):
    loop = current["state"]
    member = loop["wave"][0]
    scores = receipt(
        task_id=member["task_id"], round_id=loop["round_id"], variant_id=member["variant_id"],
        base_candidate_sha256=loop["incumbent_sha256"],
        per_view={"front": {"loss": loss, "quality_pass": True}},
    )
    if invalid:
        scores.update(validity="capture_failed", quality_pass=False, worst_view=None,
                      per_view={"front": None}, capture_receipts=[{
                          "view_id": "front", "ready": False, "decoded": False,
                      }])
    packet = {
        "object_id": "rock", "project_id": PROJECT, "score_task_id": loop["score_task_id"],
        "expected_version": current["version"], "receipts": [scores], "action": action,
        "next_variants": next_variants if next_variants is not None else [variant("next").model_dump()],
    }
    if action == "checkpoint":
        await review(handler.db, "checkpoint", digest=B, decider="supervisor")
        packet.update(review_id="checkpoint", review_revision=1, review_sha256=B)
    if action == "stop":
        packet["stop_reason"] = "The planned work is complete."
    await handler.db.add_task_context(
        loop["score_task_id"], type="note", label="note", content=SCORE_PREFIX + json.dumps(packet),
    )
    await handler.db.update_task(loop["score_task_id"], status=TaskStatus.COMPLETED)
    return packet


async def boot(handler, *, reference_kind="calibrated"):
    cooked = await cook(handler, reference_kind=reference_kind)
    bootstrap = await handler.db.get_task(cooked["task_ids"][0])
    assert bootstrap.is_blocked
    trace, tree = await sweep(handler, rule="on-formula")
    assert any(name == "object_loop_start" for name, _, _ in trace), (trace, tree)
    assert not any(g["status"] == "open" and g["gate_type"] == "event"
                   for g in await handler.db.get_gates_for_task(bootstrap.id))
    return cooked


async def test_attempt_live_and_dry_has_only_plain_brief_and_one_final_result(setup):
    from src.deliverables import resolve_task_deliverables
    from src.reviews.notifier import ReviewNotifier
    from tests.test_review_commands import _image_args
    from tests.test_review_notifier import FakeTransport

    # The policy approval is preparatory, outside this object's attempt.
    await setup.db.mark_review_notified("rev-amber-zenith", 2)
    async with setup.db.immediate() as conn:
        await conn.execute(update(doc_reviews).where(doc_reviews.c.id == "brief-review").values(
            title="Build a procedural rock",
        ))
        await conn.execute(update(doc_review_revisions).where(
            doc_review_revisions.c.review_id == "brief-review",
        ).values(content="Build a procedural rock in up to eight rounds with a $30 budget."))
    await boot(setup)
    current = await complete_wave(setup)
    candidate_id = current["state"]["wave"][0]["task_id"]
    score_id = current["state"]["score_task_id"]
    for task_id, title in ((candidate_id, "Half-depth chip-scar probe"),
                           (score_id, "rock · r0 score")):
        task = await setup.db.get_task(task_id)
        assert await resolve_task_deliverables(setup.db, task) == []
        internal = await setup.execute("review_submit", {
            "task_id": task_id, "kind": "other", "title": title,
            "content": "Internal capture receipts and continuation details.",
        })
        assert internal["success"], internal
        assert (await setup.db.get_review(internal["review_id"]))["decider"] == "supervisor"
    await score_packet(setup, current, action="stop", next_variants=[])
    await sweep(setup)
    async with setup.db._engine.connect() as conn:
        finalizer_id = (await conn.execute(select(object_loops.c.finalization_task_id))).scalar_one()
    finalizer = await setup.db.get_task(finalizer_id)
    assert not finalizer.is_blocked
    assert await resolve_task_deliverables(setup.db, finalizer) == [
        {"id": "result", "kind": "review", "target": "other"},
    ]
    evidence = json.loads(finalizer.description.split("Internal evidence (for the worker):\n")[1])
    assert evidence["baseline_capture_uri"] == "artifact://sha256/" + H
    assert evidence["best_receipt"]["capture_receipts"][0]["view_id"] == "front"
    assert "side by side" in finalizer.description
    result = await setup.execute("review_submit", {
        "task_id": finalizer_id, "kind": "other", "title": "Rock result",
        "content": "The rock looks rounder. The exact improvement was not measured. "
                   "Next time, improve the cracks. Front view images are attached below.",
    })
    assert result["success"], result
    for caption in ("Before", "After"):
        attached = await setup.execute("review_attachment_add", {
            **_image_args(result["review_id"]), "candidate_id": caption,
            "caption": caption, "view_id": "front",
        })
        assert attached["success"], attached
    # Reconcile and report retries keep the same result and do not expose
    # the worker-only evidence carried in the finalizer's description.
    await sweep(setup)
    assert len(await setup.db.list_reviews_submitted_by_task(finalizer_id)) == 1
    reviews = await setup.db.list_reviews(project_id=PROJECT)
    human = [r for r in reviews if r["kind"] == "other" and r["decider"] == "user"]
    assert {r["title"] for r in human} == {"Build a procedural rock", "Rock result"}
    attachments = await setup.db.list_review_attachments(result["review_id"], 1)
    assert {(a["view_id"], a["candidate_id"]) for a in attachments} == {
        ("front", "Before"), ("front", "After"),
    }
    transport = FakeTransport()
    notifier = ReviewNotifier(setup.db, transport, "123", "https://aq.example.test")
    assert await notifier.tick() == 2
    notices = "\n".join(content for _, content in transport.posts)
    assert "Build a procedural rock" in notices and "Rock result" in notices
    assert "r0" not in notices and "probe" not in notices and "receipts" not in notices
    assert finalizer_id not in notices
    assert "other for review: Rock result" not in notices


async def test_checkpoint_refuses_a_human_review_without_changing_the_loop(setup):
    await boot(setup)
    current = await complete_wave(setup)
    packet = await score_packet(setup, current, action="checkpoint")
    async with setup.db.immediate() as conn:
        await conn.execute(update(doc_reviews).where(doc_reviews.c.id == "checkpoint")
                           .values(decider="user"))
    before = await state(setup)
    rejected = await setup._cmd_object_score_record(packet)
    assert not rejected["success"]
    assert "supervisor decider" in rejected["error"]
    assert (await state(setup))["state"] == before["state"]
    async with setup.db.immediate() as conn:
        await conn.execute(update(doc_reviews).where(doc_reviews.c.id == "checkpoint")
                           .values(decider="supervisor"))
    await sweep(setup)
    assert (await state(setup))["state"]["checkpoint"]["review_id"] == "checkpoint"


async def test_bootstrap_restart_and_formula_discovery(setup, tmp_path):
    tmp_path = tmp_path / "seeder"
    first = ensure_default_formulas(str(tmp_path))
    assert first["created"] == ["object.md", "variation.md"]
    (tmp_path / "vault/formulas/object.md").write_text("operator edit")
    assert ensure_default_formulas(str(tmp_path))["created"] == []
    assert (tmp_path / "vault/formulas/object.md").read_text() == "operator edit"
    assert "object-loop" not in (*DEFAULT_SYSTEM_PLAYBOOK_IDS, *REQUIRED_SYSTEM_PLAYBOOK_IDS)
    cooked = await boot(setup)
    before = await state(setup)
    await sweep(setup)  # a fresh engine and timer, with no original formula event
    after = await state(setup)
    assert after["state"]["wave"] == before["state"]["wave"]
    children = await setup.db.get_children(cooked["container_id"])
    assert len(children) == 3
    async with setup.db._engine.connect() as conn:
        finalizer = (await conn.execute(select(object_loops.c.finalization_task_id))).scalar_one()
    for child in children:
        if child.id != finalizer:
            await setup.db.update_task(child.id, status=TaskStatus.COMPLETED)
    async with setup.db.immediate() as conn:
        await setup.db.settle_containers({cooked["container_id"]}, conn=conn)
    assert (await setup.db.get_task(cooked["container_id"])).status != TaskStatus.COMPLETED


@pytest.mark.parametrize("scenario", ["improve", "tie", "invalid", "repair", "budget", "plateau", "cap"])
@pytest.mark.parametrize("reference_kind", ["calibrated", "self"])
async def test_scoring_live_and_dry_traces(setup, scenario, reference_kind):
    await boot(setup, reference_kind=reference_kind)
    current = await complete_wave(setup)
    if scenario in {"tie", "plateau"}:
        async with setup.db.immediate() as conn:
            modified = copy.deepcopy(current["state"])
            modified["incumbent_loss"] = 0.1
            if scenario == "plateau":
                modified["plateau_count"] = modified["max_plateau_rounds"] - 1
            await conn.execute(update(object_loops).values(state=modified))
        current = await state(setup)
    if scenario == "cap":
        async with setup.db.immediate() as conn:
            modified = copy.deepcopy(current["state"])
            modified["round_id"] = 7
            await conn.execute(update(object_loops).values(state=modified))
        current = await state(setup)
    if scenario == "repair":
        async with setup.db.immediate() as conn:
            modified = copy.deepcopy(current["state"])
            modified["repair_count"] = modified["max_repair_rounds"]
            await conn.execute(update(object_loops).values(state=modified))
        current = await state(setup)
    next_variants = [variant("next", usd=100 if scenario == "budget" else 1).model_dump()]
    await score_packet(setup, current, invalid=scenario in {"invalid", "repair"},
                       next_variants=next_variants)
    trace, _ = await sweep(setup)
    result = await state(setup)
    assert result["state"]["reference_kind"] == reference_kind
    assert any(name == "object_score_record" for name, _, _ in trace)
    assert result["state"]["incumbent_sha256"] == (
        H if scenario in {"tie", "invalid", "repair", "plateau"} else B
    )
    if scenario in {"repair", "budget", "plateau", "cap"}:
        assert result["state"]["status"] == "stopped"
        assert "continuation refused" in result["state"]["stop_reason"]
    else:
        assert result["state"]["round_id"] == 1


@pytest.mark.parametrize("status", [TaskStatus.FAILED, TaskStatus.BLOCKED])
async def test_failed_scorer_trace(setup, status):
    await boot(setup)
    current = await complete_wave(setup)
    await setup.db.update_task(current["state"]["score_task_id"], status=status,
                               retry_count=3, max_retries=3)
    await sweep(setup, rule="on-failure")
    result = await state(setup)
    assert result["state"]["status"] == "stopped"
    assert result["state"]["incumbent_sha256"] == H
    assert result["state"]["defect_stop"]["status"] == status.value


@pytest.mark.parametrize("verdict", ["rejected", "withdrawn", "changes_requested", "approved"])
async def test_checkpoint_decision_trace(setup, verdict):
    await boot(setup)
    current = await complete_wave(setup)
    await score_packet(setup, current, action="checkpoint")
    await sweep(setup)
    async with setup.db.immediate() as conn:
        await conn.execute(update(doc_reviews).where(doc_reviews.c.id == "checkpoint")
                           .values(state=verdict))
    await sweep(setup, rule="on-review")
    result = await state(setup)
    if verdict == "approved":
        assert result["state"]["round_id"] == 1
        assert result["state"]["last_approved_checkpoint"]["candidate_sha256"] == B
    else:
        assert result["state"]["status"] == "stopped"
        assert result["state"]["checkpoint"]["candidate_sha256"] == B
        assert (await setup.db.get_review("checkpoint"))["state"] == verdict


async def test_provider_outage_holds_work_until_deadline_then_waits_for_settlement(setup):
    await boot(setup)
    current = await state(setup)
    candidate = current["state"]["wave"][0]["task_id"]
    await setup.db.update_task(candidate, status=TaskStatus.PAUSED)
    await sweep(setup)
    assert (await state(setup))["state"]["score_task_id"] is None
    async with setup.db.immediate() as conn:
        await conn.execute(update(object_loops).values(created_at=1))
    await sweep(setup)
    stopped = await state(setup)
    assert stopped["state"]["stop_reason"] == "object wall deadline reached"
    assert (await setup.db.get_task(candidate)).status == TaskStatus.PAUSED
    gates = await setup.db.list_gates(project_id=PROJECT, await_id="object:rock:terminal")
    assert gates[0]["status"] == "open"
    await setup.db.resume_task(candidate)
    await setup.db.update_task(candidate, status=TaskStatus.BLOCKED)
    await sweep(setup)
    assert (await setup.db.get_gate(gates[0]["id"]))["status"] == "resolved"


async def test_variation_is_three_levels_gated_and_never_publishes_or_settles_early(setup):
    cooked = await boot(setup)
    current = await complete_wave(setup)
    await score_packet(setup, current, action="checkpoint", next_variants=[])
    await sweep(setup)
    suite = await setup._create_task({
        "project_id": PROJECT, "parent_id": cooked["container_id"],
        "title": "Variation suite", "container": True, "task_type": "research",
    })
    assert suite["success"], suite
    suite_id = suite["task_id"]
    args = {
        "name": "variation", "project_id": PROJECT, "parent_id": suite_id,
        "vars": {
            "object_id": "rock", "candidate_sha256": B, "render_profile_sha256": H,
            "artifact_uri": "artifact://candidate", "view_set": "locked-v1",
            "seeds_a": "[101,102,103,104,105]", "seeds_b": "[106,107,108,109,110]",
            "presets": '{"low":{"scale":0.5},"high":{"scale":2}}',
        },
    }
    for key, value in (("artifact_uri", "/tmp/candidate"), ("seeds_b", "[1,1,1,1,1]"),
                       ("presets", '{"low":{"scale":NaN}}'), ("candidate_sha256", H)):
        refused = await setup._cmd_formula_cook({
            **args, "vars": {**args["vars"], key: value},
        })
        assert not refused["success"], refused
        assert not await setup.db.get_children(suite_id)
    dry = await setup._cmd_formula_cook({**args, "dry_run": True})
    assert dry["success"] and len(dry["task_ids"]) == 3, dry
    assert not await setup.db.get_children(suite_id)
    variation = await setup._cmd_formula_cook(args)
    assert variation["success"], variation
    again = await setup._cmd_formula_cook(args)
    assert not again["success"] and "already cooked" in again["error"]
    extra = await setup._create_task({
        "project_id": PROJECT, "parent_id": cooked["container_id"],
        "title": "Extra suite", "container": True, "task_type": "research",
    })
    refused = await setup._cmd_formula_cook({**args, "parent_id": extra["task_id"]})
    assert not refused["success"] and "reserve already allocated" in refused["error"]
    assert not await setup.db.get_children(extra["task_id"])
    for task_id in variation["task_ids"]:
        task = await setup.db.get_task(task_id)
        assert task.parent_task_id == suite_id
        assert len(task_id.split(".")) == 3
        assert any(g["await_id"] == "checkpoint" for g in await setup.db.get_gates_for_task(task_id))
    async with setup.db._engine.connect() as conn:
        publishable = (await conn.execute(select(tasks.c.id).where(
            tasks.c.id.in_([suite_id, *variation["task_ids"]]), _publishable_object_task(tasks),
        ))).scalars().all()
    assert publishable == []
    current = await state(setup)
    stop = await setup._cmd_object_loop_reconcile({
        "project_id": PROJECT, "object_id": "rock", "expected_version": current["version"],
        "stop_reason": "frozen generator awaits final evidence",
    })
    assert stop["outcome"] == "waiting_for_settlement"
    assert (await setup.db.get_task(cooked["container_id"])).status != TaskStatus.COMPLETED
    gate = (await setup.db.list_gates(PROJECT, await_id="object:rock:terminal"))[0]
    assert gate["status"] == "open"
    for task_id in variation["task_ids"]:
        await setup.db.update_task(task_id, status=TaskStatus.COMPLETED)
    await setup.db.update_task(suite_id, status=TaskStatus.COMPLETED)
    await sweep(setup)
    assert (await setup.db.get_gate(gate["id"]))["status"] == "resolved"


@pytest.mark.parametrize("change", ["rejected", "new_revision", "hash"])
async def test_object_formula_refuses_unapproved_proposal_atomically(setup, change):
    async with setup.db.immediate() as conn:
        if change == "hash":
            await conn.execute(update(doc_review_revisions).where(
                doc_review_revisions.c.review_id == "rev-amber-zenith",
            ).values(content_sha256=B))
        else:
            await conn.execute(update(doc_reviews).where(doc_reviews.c.id == "rev-amber-zenith")
                               .values(**({"state": "rejected"} if change == "rejected"
                                          else {"current_revision": 3})))
    for dry_run in (True, False):
        result = await setup._cmd_formula_cook({
            "name": "object", "project_id": PROJECT, "vars": variables(), "dry_run": dry_run,
        })
        assert not result["success"] and "revision 2" in result["error"], result
    assert not await setup.db.list_tasks(PROJECT)


async def test_inputs_refuse_foreign_score_scope_and_missing_capability(setup):
    await boot(setup)
    current = await complete_wave(setup)
    packet = await score_packet(setup, current)
    packet["project_id"] = "foreign"
    await setup.db.add_task_context(current["state"]["score_task_id"], type="note", label="note",
                                    content=SCORE_PREFIX + json.dumps(packet))
    result = await setup._cmd_object_loop_inputs({"project_id": PROJECT})
    assert not result["success"] and "foreign" in result["error"]
    setup._current_scope = {"kind": "session", "project_id": PROJECT, "elevated": False}
    denied = await setup._cmd_object_loop_inputs({"project_id": PROJECT})
    assert not denied["success"] and "worker" in denied["error"]
    setup._current_scope = None
    principal = ExecutionPrincipal(
        kind=PrincipalKind.PLAYBOOK, project_id=PROJECT,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["object_loop_inputs"]),
    )
    with principal_context(principal):
        foreign = await setup.execute("object_loop_inputs", {"project_id": "foreign"})
        assert not foreign["success"] and "outside" in foreign["error"]
        denied = await setup.execute("object_loop_reconcile", {"project_id": PROJECT,
                                                               "object_id": "rock"})
        assert not denied["success"] and "capability" in denied["error"]


async def test_new_review_revision_never_adopts_old_checkpoint(setup):
    await boot(setup)
    current = await complete_wave(setup)
    await score_packet(setup, current, action="checkpoint")
    await sweep(setup)
    async with setup.db.immediate() as conn:
        await conn.execute(update(doc_reviews).where(doc_reviews.c.id == "checkpoint")
                           .values(current_revision=2, state="approved"))
        await conn.execute(insert(doc_review_revisions).values(
            review_id="checkpoint", revision=2, content="changed", content_sha256=H,
            submitted_by="agent", submitted_at=2,
        ))
    await sweep(setup, rule="on-review")
    current = await state(setup)
    assert current["state"]["round_id"] == 0
    assert current["state"]["checkpoint"]["revision"] == 1
    assert not current["approved"]


async def test_two_objects_recover_in_one_sweep(setup):
    for object_id in ("rock", "stone"):
        values = variables()
        values["object_id"] = object_id
        packet = json.loads(values["start"])
        packet["object_id"] = object_id
        values["start"] = json.dumps(packet)
        result = await setup._cmd_formula_cook({
            "name": "object", "project_id": PROJECT, "vars": values,
        })
        assert result["success"], result
    trace, _ = await sweep(setup)
    assert sum(name == "object_loop_start" for name, _, _ in trace) == 2
    trace, _ = await sweep(setup)
    assert sum(name == "object_loop_reconcile" for name, _, _ in trace) == 2


async def test_object_review_approval_stores_pinned_bytes_without_activation(setup):
    from src.event_bus import EventBus
    from src.playbooks.definition import canonical_bytes

    setup.config.playbooks.enabled = True
    setup.orchestrator.bus = EventBus(env="dev")
    source = Path(setup.config.vault_root) / "projects" / PROJECT / "playbooks/object-loop.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text((BUNDLE / "source.md").read_text())
    definition = json.loads((BUNDLE / "artifact.json").read_text())
    submitted = await setup.execute("review_submit", {
        "project_id": PROJECT, "title": "Object-loop artifact review",
        "content": "# Object loop\n\nReview bounded object orchestration. Store only.",
        "playbook_id": "object-loop", "activate_on_approval": False,
        "semantic_body": json.dumps({key: definition[key] for key in ("rules", "steps")}),
    })
    assert submitted["success"], submitted
    digest = submitted["playbook"]["artifact_sha256"]
    assert await setup.db.get_playbook_artifact_row(digest) is None
    revision = await setup.db.get_review_revision(submitted["review_id"], 1)
    pinned = load_definition_json(revision["playbook_artifact"])
    assert pinned.steps == artifact().steps
    approved = await setup.execute("review_decide", {
        "review_id": submitted["review_id"], "revision": 1, "decision": "approve",
    })
    assert approved["success"] and approved["playbook"]["stored"], approved
    assert not approved["playbook"]["activated"]
    assert await setup.db.get_playbook_artifact_row(digest)
    assert canonical_bytes(pinned) == revision["playbook_artifact"].encode()
    assert await setup.db.list_playbook_activations() == []
