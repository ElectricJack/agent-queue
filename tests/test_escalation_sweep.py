"""§5.6's back-fill sweep: the pile, the dry run, and what an apply leaves behind.

The fixture is built from the five buckets §1.5 counted on the operator
database, at that shape rather than a convenient one:

* 11 ``gate`` incidents whose gate already resolved (step 1);
* 13 open ``supervisor_delivery`` incidents plus 7 already ``stale`` (step 2 --
  the stale ones are terminal, so the sweep must not touch them again);
* 10 ``needs_human`` incidents over a ``COMPLETED`` task (step 3);
* 40 task-less ``needs_human`` incidents: 28 whose ``question`` source row is
  gone, 4 in a paused project, 8 the sweep will not judge (step 4);
* 2 incidents still waiting on a live gate, which no rule may claim.

The properties pinned here are the ones the acceptance criteria name -- the dry
run matches the apply, the sweep is idempotent, and what remains open is exactly
the triage list -- plus the audit trail §5.6 asks for and the §7.1 rollback
flag.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import delete, insert

from src.config import DiscordConfig, DiscordEscalationsConfig
from src.database import Database
from src.database.tables import doc_reviews, gates
from src.doctor.escalation_checks import CHECK_ID, escalation_checks
from src.doctor.models import DoctorContext, Severity
from src.doctor.runner import apply_fix
from src.escalations.sweep import (
    AUDIT_DIRECTION,
    TARGET_OPEN_ITEMS,
    EscalationSweeper,
    SweepFacts,
)
from src.models import Project, ProjectStatus, Task, TaskStatus
from tests.db_fixtures import lease_dsn

NOW = 1_000_000.0
CHANNEL = "424242424242424242"


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def config(*, stateful: bool = True):
    return SimpleNamespace(
        discord=DiscordConfig(
            channel_id=CHANNEL,
            escalations=DiscordEscalationsConfig(stateful=stateful),
        )
    )


@pytest.fixture
async def world():
    db = Database(lease_dsn("escalation_sweep"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Active"))
    await db.create_project(Project(id="paused", name="Paused", status=ProjectStatus.PAUSED))
    clock = Clock()
    yield SimpleNamespace(db=db, clock=clock)
    await db.close()


async def seed_pile(world) -> dict[str, list[str]]:
    """Build §1.5's five buckets and return the ids by bucket."""
    db, now = world.db, world.clock()
    buckets: dict[str, list[str]] = {
        "gate_resolved": [],
        "delivery_open": [],
        "delivery_stale": [],
        "task_terminal": [],
        "retired_source": [],
        "project_inactive": [],
        "triage": [],
        "live_gate": [],
    }

    async def incident(escalation_id, **overrides):
        values = {
            "id": escalation_id,
            "project_id": "p",
            "task_id": None,
            "source_kind": "core",
            "source_identity": escalation_id,
            "incident_key": f"core:{escalation_id}",
            "supervisor_owner": "supervisor-p",
            "summary": f"old question {escalation_id}",
            "investigation": "left over from before the stateful phase",
            "decision_requested": "Does this still matter?",
            "severity": "medium",
            "now": now,
        }
        values.update(overrides)
        row, _ = await db.create_escalation(**values)
        return row

    # §1.5 bucket 1: gates whose gate is already resolved.
    for index in range(11):
        gate_id, _ = await db.create_gate("p", "human", f"Gate {index}", await_id=f"g-{index}")
        await db.resolve_gate(gate_id, resolved_by="human:jack", resolution=f"decision {index}")
        await incident(
            f"esc-gate-{index}",
            source_kind="gate",
            source_identity=gate_id,
            incident_key=f"gate:{gate_id}",
        )
        buckets["gate_resolved"].append(f"esc-gate-{index}")

    # §1.5 bucket 2: delivery notices.  The first two have a confirmed receipt,
    # which is what separates §5.6 step 2's ``notice_delivered`` from its
    # honest fallback.
    for index in range(13):
        row = await incident(
            f"esc-delivery-{index}",
            source_kind="supervisor_delivery",
            source_identity=f"task_recovery:msg-{index}",
            severity="low",
        )
        await db.enqueue_escalation_delivery(
            row["id"],
            dedup_key=f"{row['id']}:root:0:root",
            kind="root",
            payload={},
            available_at=now,
        )
        buckets["delivery_open"].append(f"esc-delivery-{index}")
    leased = {
        delivery["escalation_id"]: delivery["id"]
        for delivery in await db.claim_escalation_deliveries(lease_owner="daemon-a", now=now)
    }
    for index in range(2):
        escalation_id = buckets["delivery_open"][index]
        assert await db.finish_escalation_delivery(
            leased[escalation_id],
            lease_owner="daemon-a",
            status="sent",
            external_receipt_id=f"discord-{index}",
            channel_id=CHANNEL,
            root_message_id=f"root-{index}",
            now=now,
        ) is not None

    # The 20 the spec calls "stale" are stored terminal already.
    for index in range(7):
        row = await incident(f"esc-delivery-stale-{index}", source_kind="supervisor_delivery")
        await db.transition_escalation(
            row["id"],
            expected_revision=row["revision"],
            new_state="stale",
            terminal_outcome="stale",
            outcome="stale_expired",
            now=now,
        )
        buckets["delivery_stale"].append(f"esc-delivery-stale-{index}")

    # §1.5 bucket 3: questions whose task finished.
    for index in range(10):
        await db.create_task(
            Task(
                id=f"t-done-{index}",
                project_id="p",
                title=f"Task {index}",
                description="d",
                status=TaskStatus.COMPLETED,
            )
        )
        await incident(
            f"esc-task-{index}",
            task_id=f"t-done-{index}",
            source_kind="question",
            source_identity=f"t-done-{index}",
        )
        buckets["task_terminal"].append(f"esc-task-{index}")

    # §1.5 bucket 4: the task-less questions.  A gate incident is task-less by
    # design, and ``escalations.source_identity`` is a *soft* reference: the gate
    # row can be gone while the incident that was waiting on it lives on.  That
    # absence is the only one the sweep is allowed to call proven.
    for index in range(28):
        gate_id, _ = await db.create_gate(
            "p", "human", f"Retired gate {index}", await_id=f"retired-{index}"
        )
        await incident(
            f"esc-retired-{index}",
            source_kind="gate",
            source_identity=gate_id,
            incident_key=f"gate:{gate_id}",
        )
        async with db.immediate() as conn:
            await conn.execute(delete(gates).where(gates.c.id == gate_id))
        buckets["retired_source"].append(f"esc-retired-{index}")
    for index in range(4):
        await incident(
            f"esc-paused-{index}",
            project_id="paused",
            supervisor_owner="supervisor-paused",
            source_kind="core",
        )
        buckets["project_inactive"].append(f"esc-paused-{index}")
    for index in range(8):
        await incident(f"esc-triage-{index}", source_kind="provider_availability")
        buckets["triage"].append(f"esc-triage-{index}")

    # And two gates that are still open: no rule may claim them.
    for index in range(2):
        gate_id, _ = await db.create_gate(
            "p", "human", f"Live gate {index}", await_id=f"live-{index}"
        )
        await incident(
            f"esc-live-gate-{index}",
            source_kind="gate",
            source_identity=gate_id,
            incident_key=f"gate:{gate_id}",
        )
        buckets["live_gate"].append(f"esc-live-gate-{index}")
    return buckets


def sweeper(world, *, cfg=None, **overrides):
    return EscalationSweeper(world.db, cfg or config(), clock=world.clock, **overrides)


async def states(world, ids):
    return {
        escalation_id: (await world.db.get_escalation(escalation_id))["state"]
        for escalation_id in ids
    }


async def audit_rows(world, escalation_id):
    return [
        message
        for message in await world.db.list_escalation_messages(escalation_id)
        if message["direction"] == AUDIT_DIRECTION
    ]


async def historical_incident(world, *, task_id=None, source_kind="supervisor", identity="old"):
    row, _ = await world.db.create_escalation(
        id="esc-history", project_id="p", task_id=task_id, source_kind=source_kind,
        source_identity=identity, incident_key="history", supervisor_owner="supervisor-p",
        summary="Historical request", investigation="Preserved evidence",
        decision_requested="Does this still need action?", severity="medium", now=NOW,
    )
    return row


async def withdrawn_gate(world, *, state="withdrawn"):
    gate_id, _ = await world.db.create_gate("p", "review", "Historical draft", await_id="rev-old")
    async with world.db.immediate() as conn:
        await conn.execute(insert(doc_reviews).values(
            id="rev-old", project_id="p", kind="other", title="Historical draft",
            vault_path="projects/p/specs/old.md", current_revision=1, state=state,
            gate_id=gate_id, decider="user", created_at=NOW, updated_at=NOW,
        ))
    await historical_incident(world, source_kind="gate", identity=gate_id)
    return gate_id


async def test_withdrawn_review_notice_retires_without_resolving_its_gate(world):
    gate_id = await withdrawn_gate(world)
    engine = sweeper(world)
    plan = await engine.plan()
    assert plan.counts == {"withdrawn_review": 1}
    assert (await world.db.get_escalation("esc-history"))["state"] == "needs_human"
    report = await engine.apply(plan)
    assert report.closed == 1 and report.audited == 1
    assert (await world.db.get_gate(gate_id))["status"] == "open"
    assert (await world.db.get_review("rev-old"))["state"] == "withdrawn"
    assert (await engine.run(apply_changes=True))[1].closed == 0


async def test_pending_review_notice_remains_a_live_decision(world):
    await withdrawn_gate(world, state="in_review")
    assert (await sweeper(world).plan()).items == ()


@pytest.mark.parametrize("attach_after_plan", [False, True])
async def test_withdrawn_review_with_a_waiter_is_not_retired(world, attach_after_plan):
    gate_id = await withdrawn_gate(world)
    await world.db.create_task(Task(id="waiter", project_id="p", title="Wait", description="Wait"))
    engine = sweeper(world)
    plan = await engine.plan() if attach_after_plan else None
    async with world.db.immediate() as conn:
        await world.db.attach_gate_waiters(gate_id, ["waiter"], conn=conn)
    report = await engine.apply(plan or await engine.plan())
    assert report.closed == 0
    assert (await world.db.get_escalation("esc-history"))["state"] == "needs_human"
    assert (await world.db.get_gate(gate_id))["status"] == "open"


@pytest.mark.parametrize("status", [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED])
async def test_archived_supervisor_task_request_retires_with_its_actual_status(world, status):
    await world.db.create_task(Task(
        id="old-task", project_id="p", title="Old task", description="Old work", status=status,
    ))
    await historical_incident(world, task_id="old-task")
    assert (await sweeper(world).plan()).closable == ()
    assert await world.db.archive_task("old-task")
    engine = sweeper(world)
    plan = await engine.plan()
    assert plan.counts == {"archived_task": 1}
    assert plan.items[0].evidence["archived_status"] == status.value
    assert "not marked delivered" in plan.items[0].terminal_outcome
    assert (await engine.apply(plan)).closed == 1


async def test_archived_completed_question_uses_existing_task_terminal_rule(world):
    await world.db.create_task(Task(
        id="old-task", project_id="p", title="Old task", description="Old work",
        status=TaskStatus.COMPLETED,
    ))
    await historical_incident(world, task_id="old-task", source_kind="question")
    assert await world.db.archive_task("old-task")
    assert (await sweeper(world).plan()).counts == {"task_terminal": 1}


@pytest.mark.parametrize("missing_grant", [False, True])
async def test_legacy_worker_grants_incident_requires_both_installed_templates(
    world, tmp_path, missing_grant,
):
    import json

    cfg = config()
    cfg.vault_root = str(tmp_path)
    for profile_id in ("worker-claude", "worker-codex"):
        path = tmp_path / "agent-types" / profile_id / "profile.md"
        path.parent.mkdir(parents=True)
        commands = ["knowledge_show", "knowledge_context_deliver"]
        if missing_grant and profile_id == "worker-codex":
            commands.remove("knowledge_show")
        capabilities = {"harness_tools": ["Bash"], "aq_commands": commands, "plugin_tools": []}
        path.write_text(
            f"---\nid: {profile_id}\nname: Worker\n---\n\n## Capabilities\n\n"
            f"```json\n{json.dumps(capabilities)}\n```\n"
        )
    await historical_incident(world, identity="supervisor:worker-templates-knowledge-grants")
    plan = await sweeper(world, cfg=cfg).plan()
    assert bool(plan.closable) is not missing_grant
    if not missing_grant:
        assert plan.counts == {"worker_template_grants": 1}
        # The profile evidence can change independently of the escalation revision.
        path.unlink()
        assert (await sweeper(world, cfg=cfg).apply(plan)).closed == 0


# ======================================================================
# The dry run
# ======================================================================


async def test_the_dry_run_plans_every_bucket_and_writes_nothing(world):
    buckets = await seed_pile(world)
    before = await states(world, [i for ids in buckets.values() for i in ids])

    plan = await sweeper(world).plan()

    assert plan.skipped is None
    assert plan.counts == {
        "gate_resolved": 11,
        "task_terminal": 10,
        "supervisor_delivery": 13,
        "retired_source": 28,
        "project_inactive": 4,
        "triage": 8,
    }
    assert len(plan.closable) == 66
    assert len(plan.triage) == 8
    # The open count is exact and the terminal stale rows are not in it.
    assert plan.open_before == 76
    assert await states(world, [i for ids in buckets.values() for i in ids]) == before
    assert await audit_rows(world, "esc-gate-0") == []


async def test_the_dry_run_says_why_the_live_gates_stay_open(world):
    buckets = await seed_pile(world)
    plan = await sweeper(world).plan()

    untouched = {row["escalation_id"]: row["reason"] for row in plan.untouched}
    assert sorted(untouched) == sorted(buckets["live_gate"])
    assert set(untouched.values()) == {"a gate that is still open"}


async def test_the_plan_names_each_item_s_own_rule_and_reason(world):
    buckets = await seed_pile(world)
    plan = await sweeper(world).plan()
    items = {item.escalation_id: item for item in plan.items}

    gate = items[buckets["gate_resolved"][0]]
    assert gate.action == "resolve"
    assert gate.new_state == "resolved"
    assert gate.outcome == "gate_resolved"
    assert gate.terminal_outcome.startswith("Gate ")
    assert gate.reason == "its gate already resolved"

    delivered = items[buckets["delivery_open"][0]]
    assert delivered.action == "obsolete"
    assert delivered.outcome == "notice_delivered"
    assert delivered.evidence["delivery_statuses"] == ["sent"]

    undelivered = items[buckets["delivery_open"][-1]]
    assert undelivered.outcome == "sweep", "the audit must not claim a delivery that never happened"

    paused = items[buckets["project_inactive"][0]]
    assert paused.outcome == "sweep" and paused.evidence == {"project_status": "PAUSED"}

    assert items[buckets["triage"][0]].action == "triage"


async def test_a_paused_flag_leaves_the_pile_exactly_as_it_is(world):
    await seed_pile(world)
    plan = await sweeper(world, cfg=config(stateful=False)).plan()

    assert plan.skipped == "discord.escalations.stateful is off"
    assert plan.items == ()
    # The pile is still counted, so "how big is it" has an answer even when the
    # sweep is rolled back.
    assert plan.open_before == 76
    assert (await world.db.get_escalation("esc-gate-0"))["state"] == "needs_human"


# ======================================================================
# The apply
# ======================================================================


async def test_the_apply_does_exactly_what_the_dry_run_said(world):
    buckets = await seed_pile(world)
    engine = sweeper(world)
    plan = await engine.plan()

    report = await engine.apply(plan)

    assert report.closed == 66
    assert report.triaged == 8
    assert report.conflicts == 0 and report.failures == ()
    closed_by_plan = planned_ids(plan, action="resolve") + planned_ids(plan, action="obsolete")
    assert sorted(report.closed_ids) == sorted(closed_by_plan)

    final = await states(world, [i for ids in buckets.values() for i in ids])
    assert all(state == "resolved" for state in [final[i] for i in buckets["gate_resolved"]])
    assert all(final[i] == "cancelled" for i in buckets["delivery_open"])
    assert all(final[i] == "cancelled" for i in buckets["task_terminal"])
    assert all(final[i] == "cancelled" for i in buckets["retired_source"])
    assert all(final[i] == "cancelled" for i in buckets["project_inactive"])
    assert all(final[i] == "needs_human" for i in buckets["triage"])
    assert all(final[i] == "needs_human" for i in buckets["live_gate"])
    # The already-stale rows are untouched by the sweep entirely.
    assert all(final[i] == "stale" for i in buckets["delivery_stale"])
    assert report.open_after == 10
    assert report.open_after <= TARGET_OPEN_ITEMS


def planned_ids(plan, *, action=None):
    return [
        item.escalation_id
        for item in plan.items
        if action is None or item.action == action
    ]


async def test_every_closure_is_audited_with_the_rule_that_closed_it(world):
    buckets = await seed_pile(world)
    engine = sweeper(world)
    await engine.apply(await engine.plan())

    gate = await world.db.get_escalation(buckets["gate_resolved"][0])
    assert gate["outcome"] == "gate_resolved"
    assert gate["state"] == "resolved"
    audits = await audit_rows(world, buckets["gate_resolved"][0])
    assert len(audits) == 1
    assert audits[0]["text"].startswith("sweep: gate_resolved")
    assert audits[0]["transport"] == "sweep"
    assert audits[0]["verified_actor"] == "sweep"

    # And the sweep's audit row is never a conversation: it is neither inbound
    # nor outbound, so nothing relays it into the channel thread.
    assert all(message["direction"] == AUDIT_DIRECTION for message in audits)


async def test_the_triage_list_reaches_the_supervisor_inbox_once(world):
    buckets = await seed_pile(world)
    engine = sweeper(world)
    report = await engine.apply(await engine.plan())

    assert len(report.triage_messages) == 1
    message = await world.db.get_message(report.triage_messages[0])
    assert message.to_kind == "session" and message.to_id == "supervisor-p"
    assert message.body_kind == "escalation_sweep"
    for escalation_id in buckets["triage"]:
        assert escalation_id in message.body
    # Nothing the sweep judged is in the triage list.
    assert "esc-gate-0" not in message.body
    assert "esc-old-0" not in message.body


async def test_the_sweep_is_idempotent(world):
    await seed_pile(world)
    engine = sweeper(world)

    first = await engine.run(apply_changes=True)
    second = await engine.run(apply_changes=True)

    assert first[1].closed == 66 and first[1].triaged == 8
    assert second[0].items == ()
    assert second[1].closed == 0 and second[1].triaged == 0
    assert second[1].audited == 0
    assert second[1].triage_messages == ()
    assert second[1].open_after == first[1].open_after
    # One audit row per incident, however many times the sweep ran.
    for escalation_id in ("esc-gate-0", "esc-delivery-0", "esc-triage-0"):
        assert len(await audit_rows(world, escalation_id)) == 1


async def test_re_applying_a_stale_plan_writes_nothing(world):
    """The plan is a snapshot; a second pass re-reads before it writes."""
    await seed_pile(world)
    engine = sweeper(world)
    plan = await engine.plan()
    await engine.apply(plan)

    replay = await engine.apply(plan)

    assert replay.closed == 0 and replay.conflicts == 0
    assert replay.already == 66, "every row is already in the state the plan wanted"
    assert replay.audited == 0 and replay.triaged == 0
    assert replay.open_after == 10


async def test_a_reply_that_lands_mid_sweep_is_never_overwritten(world):
    await seed_pile(world)
    engine = sweeper(world)
    plan = await engine.plan()
    # Somebody answers one of the questions between the plan and the write.
    await world.db.accept_escalation_reply(
        "esc-task-0",
        transport="discord",
        external_message_id="msg-race",
        verified_actor="human:discord:1",
        text="answer",
        received_at=world.clock(),
    )

    report = await engine.apply(plan)

    assert report.conflicts == 1
    row = await world.db.get_escalation("esc-task-0")
    assert row["state"] == "reply_received"
    assert row["outcome"] is None
    assert await audit_rows(world, "esc-task-0") == []


# ======================================================================
# The rule table on its own, and the doctor surface
# ======================================================================


async def test_only_a_proven_absence_is_retired_and_the_rest_is_triaged(world):
    """§5.6 step 4's two halves, on the same table, side by side.

    A gate row that is gone is an absence the sweep observed, so the incident is
    obsoleted.  A ``core`` incident's source lives in no table this module can
    read, so the same missing-looking situation is *not* treated as an absence
    and the incident is listed for triage instead.
    """
    await seed_pile(world)
    plan = await sweeper(world).plan()
    by_id = {item.escalation_id: item for item in plan.items}

    retired = by_id["esc-retired-0"]
    assert retired.rule == "retired_source"
    assert retired.action == "obsolete"
    assert retired.terminal_outcome == "No longer needed: the gate it was waiting on is gone."

    unresolved = by_id["esc-triage-0"]
    assert unresolved.rule == "triage"
    assert unresolved.action == "triage"


def test_a_source_this_module_cannot_resolve_is_never_called_retired():
    """The conservative half of step 4: unknown source, unknown fate."""
    facts = SweepFacts(source_found=None)
    from src.escalations.sweep import retired_source

    row = {
        "id": "esc-1",
        "project_id": "p",
        "revision": 0,
        "source_kind": "core",
        "source_identity": "x",
    }
    assert retired_source(row, facts, None) is None


async def test_the_doctor_check_warns_while_the_sweep_has_work(world):
    await seed_pile(world)
    ctx = DoctorContext(config=config(), db=world.db)
    check = escalation_checks()[0]
    assert check.id == CHECK_ID

    result = await check.run(ctx)

    assert result.severity is Severity.WARN
    assert result.fixable is True
    assert result.data["counts"]["gate_resolved"] == 11
    assert "aq escalation sweep" in result.detail


async def test_the_doctor_fix_applies_the_plan_and_then_reports_ok(world):
    await seed_pile(world)
    ctx = DoctorContext(config=config(), db=world.db)
    check = escalation_checks()[0]

    fixed = await check.fix(ctx)
    result = await apply_fix(check, ctx)

    assert fixed.fix_applied is True
    assert fixed.data["swept"]["closed"] == 66
    # The runner re-runs the check after the fix, so what it reports is the
    # pile as it now stands: two live gates plus the eight questions the sweep
    # listed for triage, and nothing left for it to do.
    assert result.severity is Severity.OK
    assert result.data["open"] == 10
    assert result.data["counts"] == {}


async def test_the_doctor_check_is_quiet_when_the_flag_is_off(world):
    await seed_pile(world)
    ctx = DoctorContext(config=config(stateful=False), db=world.db)

    result = await escalation_checks()[0].run(ctx)

    assert result.severity is Severity.INFO
    assert "stateful" in result.detail
