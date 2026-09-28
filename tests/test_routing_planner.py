"""The pure route planner and the routing policy schema.

Spec: ``projects/agent-queue/specs/2026-09-28-mandatory-task-routing.md``
§6.3-§6.4, acceptance criterion 3.  Every test here is pure: fixture
profiles, availability, usage and load go in, a plan comes out, and nothing
touches a database, a clock or a provider.
"""

from __future__ import annotations

import random

import pytest

from src.routing.planner import (
    FALLBACK,
    PREFERRED,
    Candidate,
    ProfileFacts,
    ProviderFacts,
    Snapshot,
    TaskFacts,
    plan_route,
    reselect,
    worker_classes,
)
from src.routing.policy import PolicyError, parse_policy

#: The shipped policy (v1), verbatim from spec §6.3.
SHIPPED_POLICY = """\
version: 1
class_order: [fast-off, fast-low, fast-high, standard-low, standard-high, deep-low, deep-high]
default_kind: feature
kinds:
  design:   {class: deep-high,     max_class: deep-high,     lane: code-design}
  art:      {class: deep-high,     max_class: deep-high,     lane: art-design}
  research: {class: standard-high, max_class: deep-high}
  feature:  {class: standard-high, max_class: deep-high,     narrow: true}
  bugfix:   {class: standard-high, max_class: deep-high,     narrow: true}
  refactor: {class: standard-high, max_class: deep-high,     narrow: true}
  test:     {class: standard-high, max_class: standard-high, narrow: true}
  docs:     {class: standard-high, max_class: standard-high, narrow: true}
  chore:    {class: fast-high,     max_class: standard-high, narrow: true}
  sync:     {class: fast-high,     max_class: standard-high, narrow: true}
  plan:     {class: deep-high,     max_class: deep-high,     lane: code-design}
origins:
  integration_repair: {narrow: false}
  development_repair: {narrow: false}
  review_dispatch:    {lane: design-review}
lanes:
  code-design:   {class: deep-high, harnesses: [claude, codex], prefer: [claude]}
  art-design:    {class: deep-high, harnesses: [codex], hold: true}
  design-review: {class: deep-high, harnesses: [claude, codex]}
  narrow:
    harnesses: [opencode]
    classes: {standard-high: standard-high, fast-high: fast-low, fast-low: fast-low}
    requires: [narrow, test_verified]
    prefer: true
  narrow-unverified-model:
    harnesses: [opencode]
    classes: {fast-low: fast-off}
    requires: [narrow, test_verified, independent_verifier]
    prefer: true
reserved:
  - {class: deep-high, harness: claude, only_lanes: [code-design, design-review]}
balance:
  harness_weights: {claude: 1.0, codex: 1.0, opencode: 1.0}
  usage_soft_percent: 80
  usage_floor_factor: 0.1
  degraded_factor: 0.5
  tie_order: [codex, claude, opencode]
"""

POLICY, DIGEST = parse_policy(SHIPPED_POLICY)
CLASSES = frozenset(POLICY.class_order)
NARROW_YES = {
    "task_type": "bugfix", "intelligence_class": "standard-high", "narrow": True,
    "test_verified": True, "independent_verifier": False, "reason": "one file, has tests",
}
NARROW_NO = {**NARROW_YES, "narrow": False, "test_verified": False, "reason": "wide change"}


def _rung(class_id: str, harness: str, *, slots: int = 2, lifecycle: str = "pool",
          **changes) -> ProfileFacts:
    values = {
        "id": f"{class_id}-{harness}", "harness": harness, "provider": harness,
        "lifecycle": lifecycle, "default_class": class_id, "classes": CLASSES, "slots": slots,
    }
    values.update(changes)
    return ProfileFacts(**values)


def _fleet() -> tuple[ProfileFacts, ...]:
    return (
        _rung("deep-high", "claude"),
        _rung("deep-high", "codex"),
        _rung("standard-high", "claude", slots=4),
        _rung("standard-high", "codex", slots=4),
        _rung("standard-high", "opencode", slots=1),
        _rung("fast-low", "opencode", slots=1),
        _rung("fast-off", "opencode", slots=1),
        _rung("fast-high", "claude"),
        _rung("fast-high", "codex"),
    )


def _snapshot(profiles=None, *, out=(), degraded=(), usage=None, busy=None, backlog=None):
    providers = {}
    for key in ("claude", "codex", "opencode"):
        state = "exhausted" if key in out else "degraded" if key in degraded else "available"
        providers[key] = ProviderFacts(
            state=state, launchable=key not in out, usage_percent=(usage or {}).get(key),
        )
    return Snapshot(
        profiles=tuple(_fleet() if profiles is None else profiles),
        providers=providers, busy=dict(busy or {}), backlog=dict(backlog or {}),
    )


def _task(**changes) -> TaskFacts:
    values = {"task_id": "t-1", "title": "Fix the checkout race", "description": "…"}
    values.update(changes)
    return TaskFacts(**values)


def _plan(task: TaskFacts, snapshot: Snapshot, classification=None):
    return plan_route(
        task, POLICY, snapshot, policy_sha256=DIGEST, classification=classification,
    )


def _planned(task, snapshot, classification=None) -> dict:
    result = _plan(task, snapshot, classification)
    assert result.outcome == "planned", result
    return result.value


# -- the policy document ---------------------------------------------------------


def test_the_shipped_policy_parses_and_its_digest_is_canonical() -> None:
    assert DIGEST.startswith("sha256:") and len(DIGEST) == len("sha256:") + 64
    # Reformatting the YAML does not change the policy, so not the digest.
    reflowed = SHIPPED_POLICY.replace("{class: deep-high,     max_class", "{class: deep-high, max_class")
    assert parse_policy(reflowed)[1] == DIGEST
    changed = SHIPPED_POLICY.replace("usage_soft_percent: 80", "usage_soft_percent: 70")
    assert parse_policy(changed)[1] != DIGEST
    assert POLICY.narrow_harnesses() == {"opencode"}


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("", "non-empty"),
        ("- a\n- b\n", "mapping"),
        ("version: [", "not valid YAML"),
        (SHIPPED_POLICY.replace("version: 1", "version: 2"), "version"),
        (SHIPPED_POLICY.replace("default_kind: feature", "default_kind: nope"), "default_kind"),
        (SHIPPED_POLICY.replace("lane: art-design}", "lane: nowhere}"), "nowhere"),
        (SHIPPED_POLICY.replace("lane: art-design}", "lane: narrow}"), "narrow lane"),
        (SHIPPED_POLICY.replace("max_class: standard-high, narrow: true}\n  docs",
                                "max_class: galaxy, narrow: true}\n  docs"), "galaxy"),
        (SHIPPED_POLICY.replace("requires: [narrow, test_verified]\n",
                                "requires: [narrow, vibes]\n"), "vibes"),
        (SHIPPED_POLICY.replace("tie_order:", "tie_orders:"), "tie_orders"),
        (SHIPPED_POLICY.replace("harness_weights: {claude: 1.0", "harness_weights: {claude: 0"),
         "positive"),
    ],
    ids=lambda value: value if len(value) < 20 else None,
)
def test_an_invalid_policy_is_refused_with_a_readable_error(text: str, fragment: str) -> None:
    with pytest.raises(PolicyError, match=fragment):
        parse_policy(text)


# -- worker candidates -------------------------------------------------------------


def test_never_offers_a_template_stage_named_read_only_or_slotless_profile() -> None:
    bad = (
        _rung("standard-high", "claude", id="worker-claude", template=True),
        _rung("standard-high", "claude", id="triage"),
        _rung("standard-high", "claude", id="planner"),
        _rung("standard-high", "codex", id="resident", lifecycle="named"),
        _rung("standard-high", "codex", id="reader", read_only=True),
        _rung("standard-high", "codex", id="empty", slots=0),
        _rung("standard-high", "codex", id="off", enabled=False),
        _rung("standard-high", "codex", id="project:p:old"),
        _rung("standard-high", "claude", id="boss", runtime="supervisor"),
        _rung("standard-high", "codex", id="unmapped", classes=frozenset({"fast-low"})),
        _rung("", "codex", id="classless-pool"),
    )
    for profile in bad:
        assert worker_classes(profile) == frozenset(), profile.id
    task = _task(task_type="research")
    result = _plan(task, _snapshot(bad))
    assert result.outcome == "no_candidates"
    assert result.value["reason"] == "no_worker_candidates"

    good = _rung("standard-high", "codex", id="standard-high-codex")
    plan = _planned(task, _snapshot((*bad, good)))
    assert [c["profile_id"] for c in plan["candidates"]] == ["standard-high-codex"]


def test_a_classless_task_lifecycle_worker_runs_every_mapped_class() -> None:
    generic = _rung(
        "", "codex", id="codex-anything", lifecycle="task", slots=1,
        classes=frozenset({"standard-high", "deep-high"}),
    )
    assert worker_classes(generic) == {"standard-high", "deep-high"}
    plan = _planned(_task(task_type="research"), _snapshot((generic,)))
    assert plan["profile_id"] == "codex-anything"
    assert plan["intelligence_class"] == "standard-high"


# -- acceptance criterion 3 ----------------------------------------------------------


def test_design_goes_to_deep_high_claude_or_codex_when_claude_is_out() -> None:
    task = _task(task_type="design")
    plan = _planned(task, _snapshot())
    assert (plan["profile_id"], plan["intelligence_class"]) == ("deep-high-claude", "deep-high")
    assert plan["lane"] == "code-design" and plan["provider_intent"] == "class_only"
    assert [c["tier"] for c in plan["candidates"]] == [PREFERRED, FALLBACK]

    # Claude busier than Codex still wins while it has a free slot: prefer
    # is a tier, not a weight.
    plan = _planned(task, _snapshot(busy={"deep-high-claude": 1}))
    assert plan["profile_id"] == "deep-high-claude"

    plan = _planned(task, _snapshot(out={"claude"}))
    assert plan["profile_id"] == "deep-high-codex"
    # The unlaunchable candidate stays, for failover once Claude recovers.
    assert {c["profile_id"]: c["launchable"] for c in plan["candidates"]} == {
        "deep-high-claude": False, "deep-high-codex": True,
    }


def test_design_hint_below_the_lane_class_still_runs_at_the_lane_class() -> None:
    plan = _planned(_task(task_type="plan", class_hint="standard-high"), _snapshot())
    assert plan["intelligence_class"] == "deep-high"
    assert plan["profile_id"] == "deep-high-claude"


def test_art_goes_to_deep_high_codex_pinned_and_holds_when_codex_is_out() -> None:
    task = _task(task_type="art")
    plan = _planned(task, _snapshot())
    assert (plan["profile_id"], plan["provider_intent"]) == ("deep-high-codex", "pinned")
    assert plan["lane"] == "art-design"

    held = _plan(task, _snapshot(out={"codex"}))
    assert held.outcome == "held"
    assert held.value["providers"] == ["codex"]
    assert [c["profile_id"] for c in held.value["candidates"]] == ["deep-high-codex"]


@pytest.mark.parametrize("busy", [{}, {"deep-high-codex": 2}, {"deep-high-codex": 9}])
def test_a_deep_high_bugfix_never_goes_to_claude(busy) -> None:
    task = _task(task_type="bugfix", class_hint="deep-high")
    plan = _planned(task, _snapshot(busy=busy), NARROW_NO)
    assert plan["profile_id"] == "deep-high-codex"
    assert "deep-high-claude" not in {c["profile_id"] for c in plan["candidates"]}
    # With Codex out it holds rather than taking the reserved Claude cell.
    assert _plan(task, _snapshot(out={"codex"}), NARROW_NO).outcome == "held"


def test_narrow_test_verified_work_goes_to_opencode_while_it_has_a_free_slot() -> None:
    task = _task(task_type="bugfix")
    # Without a classification the narrow lane could change the route.
    asked = _plan(task, _snapshot())
    assert asked.outcome == "needs_classification"
    assert asked.value["questions"] == ["narrow", "test_verified"]
    assert "bugfix" in asked.value["allowed_kinds"]
    assert asked.value["allowed_classes"] == list(POLICY.class_order)

    # OpenCode's free slot wins even at a higher pressure than an idle Codex.
    plan = _planned(task, _snapshot(backlog={"standard-high-codex": 0}), NARROW_YES)
    assert plan["profile_id"] == "standard-high-opencode"
    assert plan["lane"] == "narrow"
    assert plan["candidates"][0]["tier"] == PREFERRED

    # Full (1/1): the lowest-pressure general profile.
    full = _snapshot(busy={"standard-high-opencode": 1, "standard-high-claude": 1})
    plan = _planned(task, full, NARROW_YES)
    assert plan["profile_id"] == "standard-high-codex"
    assert "fell back" in plan["reason"]

    # Not narrow: OpenCode is not even a candidate.
    plan = _planned(task, _snapshot(), NARROW_NO)
    assert "opencode" not in {c["harness"] for c in plan["candidates"]}


def test_narrow_lanes_map_the_class() -> None:
    chore = _task(task_type="chore")  # fast-high by default
    plan = _planned(chore, _snapshot(), {**NARROW_YES, "task_type": "chore",
                                         "intelligence_class": "fast-high"})
    assert (plan["profile_id"], plan["intelligence_class"]) == ("fast-low-opencode", "fast-low")

    verified = {**NARROW_YES, "task_type": "chore", "independent_verifier": True}
    plan = _planned(_task(task_type="chore", class_hint="fast-low"), _snapshot(), verified)
    # Both narrow lanes apply; the first-declared lane's candidate wins the tie.
    assert [c["profile_id"] for c in plan["candidates"][:2]] == [
        "fast-low-opencode", "fast-off-opencode",
    ]
    plan = _planned(
        _task(task_type="chore", class_hint="fast-low"),
        _snapshot(busy={"fast-low-opencode": 1}), verified,
    )
    assert (plan["profile_id"], plan["intelligence_class"]) == ("fast-off-opencode", "fast-off")


def test_no_opencode_profile_means_no_classification_call() -> None:
    fleet = [p for p in _fleet() if p.harness != "opencode"]
    plan = _planned(_task(task_type="bugfix"), _snapshot(fleet))
    assert plan["classification"] is None
    assert plan["profile_id"] == "standard-high-codex"  # tie_order puts Codex first


@pytest.mark.parametrize("origin", ["integration_repair", "development_repair"])
def test_a_repair_never_goes_to_opencode(origin) -> None:
    task = _task(task_type="bugfix", created_by_kind=origin)
    # Narrow is off for the origin, so no classification is needed ...
    plan = _planned(task, _snapshot(busy={"standard-high-codex": 4, "standard-high-claude": 4}))
    assert plan["rule"] == f"kinds.bugfix+origins.{origin}"
    assert "opencode" not in {c["harness"] for c in plan["candidates"]}
    # ... and a classification claiming narrow work changes nothing.
    plan = _planned(task, _snapshot(), NARROW_YES)
    assert plan["provider"] != "opencode"
    assert "opencode" not in {c["harness"] for c in plan["candidates"]}


def test_work_moves_away_from_a_provider_above_the_usage_soft_limit() -> None:
    task = _task(task_type="research")
    # Claude idle and Codex a little busy: Claude wins on load alone.
    calm = _snapshot(busy={"standard-high-codex": 1})
    assert _planned(task, calm)["profile_id"] == "standard-high-claude"
    # Claude near its window: its capacity shrinks and Codex takes the work.
    hot = _snapshot(busy={"standard-high-codex": 1}, usage={"claude": 95})
    plan = _planned(task, hot)
    assert plan["profile_id"] == "standard-high-codex"
    claude = next(s for s in plan["scores"] if s["profile_id"] == "standard-high-claude")
    assert claude["usage_percent"] == 95 and claude["usage_factor"] == pytest.approx(0.325)
    # At or below the soft limit, usage is ignored.
    assert _planned(task, _snapshot(busy={"standard-high-codex": 1}, usage={"claude": 80}))[
        "profile_id"
    ] == "standard-high-claude"


def test_a_degraded_provider_counts_at_its_degraded_factor() -> None:
    task = _task(task_type="research")
    plan = _planned(task, _snapshot(degraded={"codex"}))
    assert plan["profile_id"] == "standard-high-claude"
    codex = next(s for s in plan["scores"] if s["profile_id"] == "standard-high-codex")
    assert codex["avail_factor"] == 0.5


def test_exclude_providers_steers_a_review_to_the_other_family() -> None:
    review = _task(task_type="research", created_by_kind="review_dispatch",
                   exclude_providers=frozenset({"claude"}))
    plan = _planned(review, _snapshot())
    assert (plan["profile_id"], plan["lane"]) == ("deep-high-codex", "design-review")
    other = _planned(_task(task_type="research", created_by_kind="review_dispatch",
                           exclude_providers=frozenset({"codex"})), _snapshot())
    # design-review is a lane the reserved Claude cell admits.
    assert other["profile_id"] == "deep-high-claude"
    only = _plan(_task(task_type="art", exclude_providers=frozenset({"codex"})), _snapshot())
    assert only.outcome == "no_candidates"
    assert only.value["reason"] == "excluded_providers"


def test_preferred_provider_filters_candidates() -> None:
    task = _task(task_type="research", preferred_provider="claude")
    assert _planned(task, _snapshot())["profile_id"] == "standard-high-claude"
    art = _plan(_task(task_type="art", preferred_provider="claude"), _snapshot())
    assert art.outcome == "no_candidates"
    assert art.value["reason"] == "preferred_provider_unavailable"


def test_a_non_pool_workspace_gets_task_lifecycle_profiles_only() -> None:
    task = _task(task_type="research", needs_task_lifecycle=True)
    refused = _plan(task, _snapshot())
    assert refused.outcome == "no_candidates"
    assert refused.value["reason"] == "workspace_requirement"
    push = _rung("standard-high", "claude", id="claude-push", lifecycle="task", slots=1)
    plan = _planned(task, _snapshot((*_fleet(), push)))
    assert [c["profile_id"] for c in plan["candidates"]] == ["claude-push"]


def test_held_comes_before_classification() -> None:
    task = _task()  # no kind, no hint: would need classification
    held = _plan(task, _snapshot(out={"claude", "codex", "opencode"}))
    assert held.outcome == "held"
    assert held.value["providers"] == ["claude", "codex", "opencode"]
    asked = _plan(task, _snapshot())
    assert asked.outcome == "needs_classification"
    assert asked.value["questions"][:2] == ["task_type", "intelligence_class"]


def test_ties_break_deterministically() -> None:
    task = _task(task_type="research")
    fleet = list(_fleet())
    choices = set()
    for seed in range(8):
        random.Random(seed).shuffle(fleet)
        plan = _planned(task, _snapshot(fleet))
        choices.add(plan["profile_id"])
        assert [c["profile_id"] for c in plan["candidates"]] == [
            "standard-high-codex", "standard-high-claude",
        ]
    # Equal pressure: tie_order puts Codex first.
    assert choices == {"standard-high-codex"}


# -- hints, clamps and the classification ---------------------------------------------


def test_the_hint_beats_the_classification_and_max_class_clamps() -> None:
    plan = _planned(
        _task(task_type="docs", class_hint="deep-high"), _snapshot(), NARROW_NO,
    )
    assert plan["intelligence_class"] == "standard-high"
    assert plan["class_clamped_from"] == "deep-high"
    assert "clamped from deep-high" in plan["reason"]

    classified = {**NARROW_NO, "task_type": "research", "intelligence_class": "deep-high"}
    plan = _planned(_task(class_hint="standard-high"), _snapshot(), classified)
    assert plan["task_type"] == "research"
    assert plan["intelligence_class"] == "standard-high"
    plan = _planned(_task(), _snapshot(), classified)
    assert plan["intelligence_class"] == "deep-high"
    assert plan["profile_id"] == "deep-high-codex"


def test_an_unknown_hint_is_ignored_with_a_note() -> None:
    plan = _planned(_task(task_type="research", class_hint="galaxy-brain"), _snapshot())
    assert plan["intelligence_class"] == "standard-high"
    assert "galaxy-brain" in plan["reason"]


@pytest.mark.parametrize(
    "classification",
    [
        {"failed": True},
        {**NARROW_YES, "task_type": "poetry"},
        {**NARROW_YES, "intelligence_class": "galaxy-brain"},
        {**NARROW_YES, "narrow": "yes"},
        "narrow please",
    ],
)
def test_a_failed_classification_routes_with_the_defaults(classification) -> None:
    plan = _planned(_task(), _snapshot(), classification)
    assert plan["task_type"] == "feature"
    assert plan["intelligence_class"] == "standard-high"
    assert plan["classification"]["failed"] is True
    # Every requires flag reads false: no OpenCode.
    assert "opencode" not in {c["harness"] for c in plan["candidates"]}


def test_the_plan_names_its_rule_policy_and_balance() -> None:
    plan = _planned(_task(task_type="research"), _snapshot())
    assert plan["rule"] == "kinds.research"
    assert plan["policy_sha256"] == DIGEST
    assert plan["balance"]["tie_order"] == ["codex", "claude", "opencode"]
    assert plan["reason"].startswith("kind research, class standard-high")
    assert {s["profile_id"] for s in plan["scores"]} == {
        "standard-high-codex", "standard-high-claude",
    }


# -- apply's re-selection ----------------------------------------------------------------


def test_reselection_spreads_a_burst_planned_against_one_snapshot() -> None:
    task = _task(task_type="research")
    stale = _planned(task, _snapshot())
    candidates = [Candidate.from_dict(c) for c in stale["candidates"]]
    backlog: dict[str, int] = {}
    chosen = []
    for _ in range(8):
        selection = reselect(candidates, _snapshot(backlog=backlog), POLICY.balance)
        assert selection is not None
        chosen.append(selection.chosen.profile_id)
        backlog[selection.chosen.profile_id] = backlog.get(selection.chosen.profile_id, 0) + 1
    # Every plan said Codex; applied one by one they alternate.
    assert stale["profile_id"] == "standard-high-codex"
    assert chosen.count("standard-high-codex") == chosen.count("standard-high-claude") == 4


def test_reselection_drops_unlaunchable_candidates_and_returns_none_when_empty() -> None:
    plan = _planned(_task(task_type="research"), _snapshot())
    candidates = [Candidate.from_dict(c) for c in plan["candidates"]]
    selection = reselect(candidates, _snapshot(out={"codex"}), POLICY.balance)
    assert selection is not None and selection.chosen.profile_id == "standard-high-claude"
    assert reselect(candidates, _snapshot(out={"codex", "claude"}), POLICY.balance) is None
