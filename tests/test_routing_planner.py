"""The pure route planner and the routing policy schema.

Spec: ``projects/agent-queue/specs/2026-09-28-mandatory-task-routing.md``
§6.3-§6.4, acceptance criterion 3.  Every test here is pure: fixture
profiles, availability, usage and load go in, a plan comes out, and nothing
touches a database, a clock or a provider.
"""

from __future__ import annotations

import json
import random
from dataclasses import fields, replace
from pathlib import Path

import pytest

from src.routing.planner import (
    FALLBACK,
    PREFERRED,
    Candidate,
    ProfileFacts,
    ProviderFacts,
    Snapshot,
    TaskFacts,
    is_candidate,
    plan_route,
    read_classification,
    reselect,
    worker_classes,
)
from src.routing.policy import RISK_LEVELS, PolicyError, parse_policy, selector_matches

#: Original balanced policy; retained to check backwards compatibility and replay.
BALANCED_POLICY = """\
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
  narrow-hosted:
    harnesses: [opencode-zen, opencode-zen-nemotron, opencode-zen-longcat]
    classes: {standard-high: standard-high}
    requires: [narrow, test_verified]
    prefer: true
reserved:
  - {class: deep-high, harness: claude, only_lanes: [code-design, design-review]}
balance:
  harness_weights: {claude: 1.0, codex: 1.0, opencode: 1.0, opencode-zen: 1.0, opencode-zen-nemotron: 1.0, opencode-zen-longcat: 1.0}
  usage_soft_percent: 80
  usage_floor_factor: 0.1
  degraded_factor: 0.5
  tie_order: [codex, claude, opencode, opencode-zen, opencode-zen-nemotron, opencode-zen-longcat]
"""

SHIPPED_POLICY = BALANCED_POLICY.replace(
    "narrow: true}", "narrow: true, prefer_harnesses: [codex]}",
).replace(
    "harnesses: [opencode-zen, opencode-zen-nemotron, opencode-zen-longcat]",
    "harnesses: [opencode-zen*]",
)

POLICY, DIGEST = parse_policy(BALANCED_POLICY)
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
        # OpenCode on Ollama: a self-hosted model (``local_models``).
        _rung("standard-high", "opencode", slots=1, local=True),
        _rung("fast-low", "opencode", slots=1, local=True),
        _rung("fast-off", "opencode", slots=1, local=True),
        _rung("fast-high", "claude"),
        _rung("fast-high", "codex"),
    )


def _snapshot(profiles=None, *, out=(), degraded=(), usage=None, busy=None, backlog=None):
    providers = {}
    for key in ("claude", "codex", "opencode",
                "opencode-zen", "opencode-zen-nemotron", "opencode-zen-longcat"):
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


def test_benchmark_selector_pins_allowlisted_class_and_harness() -> None:
    policy, digest = parse_policy(BALANCED_POLICY + """
benchmark_arms:
  opus55:
    class: deep-high
    harness: claude
    requested_model: claude-opus-5-5
    observed_models: [claude-opus-5-5*]
""")
    task = _task(task_type="feature", benchmark_arms=("opus55",))
    result = plan_route(task, policy, _snapshot(), policy_sha256=digest)
    assert result.outcome == "planned"
    assert result.value["benchmark_arm"] == "opus55"
    assert result.value["profile_id"] == "deep-high-claude"
    assert result.value["provider_intent"] == "pinned"
    assert result.value["requested_model"] == "claude-opus-5-5"
    held = plan_route(task, policy, _snapshot(out=("claude",)), policy_sha256=digest)
    assert held.outcome == "held"
    assert held.value["benchmark_arm"] == "opus55"
    assert plan_route(_task(benchmark_arms=("unknown",)), policy, _snapshot(),
                      policy_sha256=digest).outcome == "no_candidates"
    assert plan_route(_task(benchmark_arms=("opus55", "unknown")), policy, _snapshot(),
                      policy_sha256=digest).outcome == "no_candidates"


# -- the policy document ---------------------------------------------------------


def test_the_shipped_policy_parses_and_its_digest_is_canonical() -> None:
    assert DIGEST.startswith("sha256:") and len(DIGEST) == len("sha256:") + 64
    # Reformatting the YAML does not change the policy, so not the digest.
    reflowed = BALANCED_POLICY.replace("{class: deep-high,     max_class", "{class: deep-high, max_class")
    assert parse_policy(reflowed)[1] == DIGEST
    changed = BALANCED_POLICY.replace("usage_soft_percent: 80", "usage_soft_percent: 70")
    assert parse_policy(changed)[1] != DIGEST
    assert POLICY.narrow_harnesses() == {
        "opencode", "opencode-zen", "opencode-zen-nemotron", "opencode-zen-longcat",
    }


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("", "non-empty"),
        ("- a\n- b\n", "mapping"),
        ("version: [", "not valid YAML"),
        (BALANCED_POLICY.replace("version: 1", "version: 2"), "version"),
        (BALANCED_POLICY.replace("default_kind: feature", "default_kind: nope"), "default_kind"),
        (BALANCED_POLICY.replace("lane: art-design}", "lane: nowhere}"), "nowhere"),
        (BALANCED_POLICY.replace("lane: art-design}", "lane: narrow}"), "narrow lane"),
        (BALANCED_POLICY.replace("max_class: standard-high, narrow: true}\n  docs",
                                "max_class: galaxy, narrow: true}\n  docs"), "galaxy"),
        (BALANCED_POLICY.replace("requires: [narrow, test_verified]\n",
                                "requires: [narrow, vibes]\n"), "vibes"),
        (BALANCED_POLICY.replace("tie_order:", "tie_orders:"), "tie_orders"),
        (BALANCED_POLICY.replace("harness_weights: {claude: 1.0", "harness_weights: {claude: 0"),
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


def _hosted_fleet() -> tuple[ProfileFacts, ...]:
    """The fleet plus one hosted OpenCode rung: OpenCode Zen, a pool of one."""
    return (*_fleet(), _rung("standard-high", "opencode-zen", slots=1))


@pytest.mark.parametrize("origin", ["integration_repair", "development_repair"])
@pytest.mark.parametrize("classification", [None, NARROW_YES])
def test_repair_excludes_every_zen_variant_even_when_hosted_workers_are_busy(
    origin, classification,
) -> None:
    policy, digest = parse_policy(SHIPPED_POLICY)
    zen = [
        _rung("standard-high", harness, slots=1)
        for harness in (
            "opencode-zen", "opencode-zen-nemotron", "opencode-zen-longcat",
            "opencode-zen-new-preview", "opencode-zenfuture",
        )
    ]
    result = plan_route(
        _task(task_type="bugfix", class_hint="standard-high", created_by_kind=origin),
        policy, _snapshot((*_fleet(), *zen), busy={
            "standard-high-codex": 4, "standard-high-claude": 4,
        }), policy_sha256=digest, classification=classification,
    )
    assert result.outcome == "planned", result
    assert result.value["rule"] == f"kinds.bugfix+origins.{origin}"
    assert {c["harness"] for c in result.value["candidates"]} == {"codex", "claude"}
    assert result.value["profile_id"] in {"standard-high-codex", "standard-high-claude"}


def test_new_zen_variant_is_reachable_only_through_the_narrow_hosted_lane() -> None:
    policy, digest = parse_policy(SHIPPED_POLICY)
    snapshot = _snapshot((
        *_fleet(), _rung("standard-high", "opencode-zen-new-preview", slots=1),
    ), busy={"standard-high-opencode": 1})
    result = plan_route(_task(task_type="bugfix"), policy, snapshot,
                        policy_sha256=digest, classification=NARROW_YES)
    assert result.outcome == "planned", result
    assert (result.value["profile_id"], result.value["lane"]) == (
        "standard-high-opencode-zen-new-preview", "narrow-hosted",
    )
    result = plan_route(_task(task_type="bugfix"), policy, snapshot,
                        policy_sha256=digest, classification=NARROW_NO)
    assert result.outcome == "planned", result
    assert {c["harness"] for c in result.value["candidates"]} == {"codex", "claude"}


def test_a_selector_names_one_harness_or_a_prefix() -> None:
    assert selector_matches("opencode-zen", "opencode-zen")
    assert selector_matches("opencode-zenfuture", "opencode-zen*")
    assert selector_matches("opencode-zen", "opencode-zen*")
    assert not selector_matches("opencode-zen", "opencode")
    assert not selector_matches("opencode", "opencode-zen*")


@pytest.mark.parametrize(
    "selector",
    ["opencode-zen[", "opencode-zen[ab]", "opencode-zen?", "*zen", "open*code",
     "opencode-zen**", ""],
)
def test_a_malformed_lane_selector_stops_the_policy(selector: str) -> None:
    # fnmatch read "opencode-zen[" as a literal that matched no harness, so a
    # Zen rung fell into a repair's general candidates; now it is no policy.
    text = SHIPPED_POLICY.replace("harnesses: [opencode-zen*]", f"harnesses: ['{selector}']")
    assert text != SHIPPED_POLICY
    with pytest.raises(PolicyError, match="harness selectors"):
        parse_policy(text)


def test_a_design_lane_prefers_exact_harnesses_only() -> None:
    text = BALANCED_POLICY.replace(
        "harnesses: [claude, codex], prefer: [claude]",
        "harnesses: [claude*, codex], prefer: [claude*]",
    )
    assert text != BALANCED_POLICY
    with pytest.raises(PolicyError, match="prefer"):
        parse_policy(text)
    parse_policy(text.replace("prefer: [claude*]", "prefer: [claude]"))


def test_lane_selectors_matching_no_installed_harness_are_reported() -> None:
    policy, digest = parse_policy(SHIPPED_POLICY)
    fleet = [p for p in _fleet() if p.harness != "opencode"]
    snapshot = replace(_snapshot(fleet), harnesses=frozenset({"claude", "codex", "gemini"}))
    result = plan_route(_task(task_type="bugfix"), policy, snapshot, policy_sha256=digest,
                        classification=NARROW_NO)
    # An install without OpenCode still routes; the report names every selector.
    assert result.outcome == "planned", result
    assert (
        "lane selectors match no installed harness: narrow:opencode, "
        "narrow-unverified-model:opencode, narrow-hosted:opencode-zen*"
    ) in result.value["reason"]

    snapshot = replace(
        _snapshot(_hosted_fleet()),
        harnesses=frozenset({"claude", "codex", "opencode", "opencode-zen"}),
    )
    result = plan_route(_task(task_type="bugfix"), policy, snapshot, policy_sha256=digest,
                        classification=NARROW_NO)
    assert result.outcome == "planned", result
    assert "match no installed harness" not in result.value["reason"]


def test_hosted_opencode_takes_narrow_work_when_local_opencode_is_full() -> None:
    task = _task(task_type="bugfix")
    # Both OpenCode lanes are the preferred tier; the local one is declared first.
    plan = _planned(task, _snapshot(_hosted_fleet()), NARROW_YES)
    assert plan["profile_id"] == "standard-high-opencode"
    assert [c["profile_id"] for c in plan["candidates"][:2]] == [
        "standard-high-opencode", "standard-high-opencode-zen",
    ]
    assert {c["tier"] for c in plan["candidates"][:2]} == {PREFERRED}

    # Local OpenCode full (1/1): the hosted lane, at the task's own class.
    busy = _snapshot(_hosted_fleet(), busy={"standard-high-opencode": 1})
    plan = _planned(task, busy, NARROW_YES)
    assert (plan["profile_id"], plan["intelligence_class"]) == (
        "standard-high-opencode-zen", "standard-high",
    )
    assert plan["lane"] == "narrow-hosted"

    # Both full: the general tier, never past a free Claude or Codex slot.
    full = _snapshot(_hosted_fleet(), busy={
        "standard-high-opencode": 1, "standard-high-opencode-zen": 1,
    })
    plan = _planned(task, full, NARROW_YES)
    assert plan["profile_id"] in {"standard-high-codex", "standard-high-claude"}
    assert "fell back" in plan["reason"]


def test_hosted_and_local_opencode_are_separate_providers() -> None:
    task = _task(task_type="bugfix")
    # Local Ollama out says nothing about the hosted gateway, and vice versa.
    plan = _planned(task, _snapshot(_hosted_fleet(), out={"opencode"}), NARROW_YES)
    assert plan["profile_id"] == "standard-high-opencode-zen"
    plan = _planned(task, _snapshot(_hosted_fleet(), out={"opencode-zen"}), NARROW_YES)
    assert plan["profile_id"] == "standard-high-opencode"


@pytest.mark.parametrize(
    ("task", "classification"),
    [
        # Not narrow: hosted OpenCode is not even a candidate.
        (_task(task_type="bugfix"), NARROW_NO),
        # A repair is never OpenCode work, whatever the classification says.
        (_task(task_type="bugfix", created_by_kind="integration_repair"), NARROW_YES),
        (_task(task_type="bugfix", created_by_kind="development_repair"), NARROW_YES),
        # The lane maps standard-high only: cheap and deep work never reach it.
        (_task(task_type="chore"), {**NARROW_YES, "task_type": "chore",
                                     "intelligence_class": "fast-high"}),
        (_task(task_type="bugfix", class_hint="deep-high"), NARROW_YES),
        # Narrow but not test-verified.
        (_task(task_type="bugfix"), {**NARROW_YES, "test_verified": False}),
    ],
    ids=["not-narrow", "integration-repair", "development-repair", "fast", "deep",
         "unverified"],
)
def test_hosted_opencode_gets_only_narrow_test_verified_standard_high_work(
    task, classification
) -> None:
    busy = {"standard-high-opencode": 1, "fast-low-opencode": 1}
    plan = _planned(task, _snapshot(_hosted_fleet(), busy=busy), classification)
    assert "opencode-zen" not in {c["harness"] for c in plan["candidates"]}
    assert plan["profile_id"] != "standard-high-opencode-zen"


def _local(plan: dict) -> set[str]:
    return {c["profile_id"] for c in plan["candidates"] if c["local"]}


def test_priority_is_not_a_routing_input() -> None:
    # rev-wise-impact (2026-10-09): a task's priority never dictates the
    # model.  The planner is not given it, and no lane or local-model rule
    # may name it.
    assert "priority" not in {field.name for field in fields(TaskFacts)}
    for text, path in (
        (SHIPPED_POLICY + "local_models: {above_priority: 50}\n", "local_models.above_priority"),
        (LANE_CAP_POLICY.replace("max_risk: low\n", "max_risk: low\n    above_priority: 99\n"),
         "lanes.narrow.above_priority"),
    ):
        with pytest.raises(PolicyError, match=path):
            parse_policy(text)


@pytest.mark.parametrize("classification", [NARROW_YES, NARROW_NO])
def test_a_train_bugfix_never_picks_a_local_profile(classification) -> None:
    # Every hosted rung full: the local model would win on load, and still is
    # not a candidate.
    busy = {
        "standard-high-claude": 4, "standard-high-codex": 4, "standard-high-opencode-zen": 1,
    }
    task = _task(task_type="bugfix", on_train=True)
    plan = _planned(task, _snapshot(_hosted_fleet(), busy=busy), classification)
    assert _local(plan) == set()
    assert plan["profile_id"] != "standard-high-opencode"


def test_a_narrow_docs_task_runs_on_a_local_profile() -> None:
    task = _task(task_type="docs")
    plan = _planned(task, _snapshot(), {**NARROW_YES, "task_type": "docs"})
    assert plan["profile_id"] == "standard-high-opencode"
    assert plan["candidates"][0]["local"] is True


@pytest.mark.parametrize(
    ("changes", "local_allowed"),
    [
        ({}, True),
        ({"on_train": True}, False),
        # Only the policy's train kinds lose the local model on a train.
        ({"on_train": True, "task_type": "docs"}, True),
        ({"blocks_work": True}, False),
    ],
    ids=["default", "train-bugfix", "train-docs", "blocking"],
)
def test_the_local_model_gate(changes, local_allowed) -> None:
    values = {"task_type": "bugfix", **changes}
    classification = {**NARROW_YES, "task_type": values["task_type"]}
    plan = _planned(_task(**values), _snapshot(), classification)
    assert bool(_local(plan)) is local_allowed
    assert (plan["profile_id"] == "standard-high-opencode") is local_allowed


def test_the_gate_reads_the_classified_kind() -> None:
    # A kindless task on a train, classified as a bugfix, is a train bugfix.
    plan = _planned(_task(on_train=True), _snapshot(), NARROW_YES)
    assert plan["task_type"] == "bugfix"
    assert _local(plan) == set()


def test_local_models_policy_knobs() -> None:
    policy, digest = parse_policy(BALANCED_POLICY + (
        "local_models:\n"
        "  harnesses: [opencode-zen]\n"
        "  train_kinds: []\n"
        "  allow_blocking: true\n"
    ))

    def plan(task):
        result = plan_route(
            task, policy, _snapshot(_hosted_fleet()), policy_sha256=digest,
            classification=NARROW_YES,
        )
        assert result.outcome == "planned", result
        return result.value

    def candidates(task):
        return {c["profile_id"] for c in plan(task)["candidates"]}

    local = {"standard-high-opencode", "standard-high-opencode-zen"}
    # No train kinds and allow_blocking admit this task ...
    task = _task(task_type="bugfix", on_train=True, blocks_work=True)
    assert local <= candidates(task)
    # ... and a harness the policy names is local, behind the default gate, too.
    gated, gated_digest = parse_policy(BALANCED_POLICY + (
        "local_models: {harnesses: [opencode-zen]}\n"
    ))
    result = plan_route(task, gated, _snapshot(_hosted_fleet()), policy_sha256=gated_digest,
                        classification=NARROW_YES)
    assert result.outcome == "planned", result
    assert not {c["profile_id"] for c in result.value["candidates"]} & local


# -- hosted lanes: all three (Space Bunny + Nemotron + LongCat) -------------


def _three_hosted_fleet() -> tuple[ProfileFacts, ...]:
    """The fleet plus all three hosted OpenCode rungs (one slot each)."""
    return (
        *_fleet(),
        _rung("standard-high", "opencode-zen",          slots=1),
        _rung("standard-high", "opencode-zen-nemotron", slots=1),
        _rung("standard-high", "opencode-zen-longcat",  slots=1),
    )


def test_three_hosted_lanes_route_in_tie_order_and_independent_disable() -> None:
    """The local rung wins first; the hosted rungs follow tie_order.

    Each hosted rung is an independent availability row: failing one does
    not disable the others."""
    task = _task(task_type="bugfix")
    fleet = _three_hosted_fleet()

    # All three hosted rungs free: tie_order puts opencode-zen first.
    # The local opencode rung is also PREFERRED and declared first in the
    # narrow lane, so it wins overall.
    plan = _planned(task, _snapshot(fleet), NARROW_YES)
    assert plan["profile_id"] == "standard-high-opencode"

    # Local full: the three hosted rungs must appear in tie_order order.
    busy = _snapshot(fleet, busy={"standard-high-opencode": 1})
    plan = _planned(task, busy, NARROW_YES)
    hosted_profiles = [
        c["profile_id"] for c in plan["candidates"] if c["tier"] == PREFERRED
    ]
    assert hosted_profiles == [
        "standard-high-opencode",
        "standard-high-opencode-zen",
        "standard-high-opencode-zen-nemotron",
        "standard-high-opencode-zen-longcat",
    ], hosted_profiles
    assert plan["profile_id"] == "standard-high-opencode-zen"
    assert plan["lane"] == "narrow-hosted"

    # Space Bunny out (availability row opencode-zen): the other two still route.
    out_z = _snapshot(fleet, busy={"standard-high-opencode": 1},
                      out={"opencode-zen"})
    plan = _planned(task, out_z, NARROW_YES)
    assert plan["profile_id"] == "standard-high-opencode-zen-nemotron"

    # Nemotron and Space Bunny both out: LongCat still routes.
    out_both = _snapshot(fleet, busy={"standard-high-opencode": 1},
                         out={"opencode-zen", "opencode-zen-nemotron"})
    plan = _planned(task, out_both, NARROW_YES)
    assert plan["profile_id"] == "standard-high-opencode-zen-longcat"

    # All three hosted rungs out: local opencode wins (PREFERRED, declared first).
    out_all = _snapshot(fleet, out={"opencode-zen", "opencode-zen-nemotron",
                                    "opencode-zen-longcat"})
    plan = _planned(task, out_all, NARROW_YES)
    assert plan["profile_id"] == "standard-high-opencode"


def test_three_hosted_lanes_are_each_their_own_availability_row() -> None:
    """Each hosted rung has a provider row named after its harness id, so
    failing one does not affect the other two."""
    task = _task(task_type="bugfix")
    fleet = _three_hosted_fleet()

    # Each harness id is its own provider key.
    keys = {p.id for p in fleet if p.harness.startswith("opencode-zen")}
    assert keys == {
        "standard-high-opencode-zen",
        "standard-high-opencode-zen-nemotron",
        "standard-high-opencode-zen-longcat",
    }

    # Failing one key leaves the other two launchable.
    for out_key in ("opencode-zen", "opencode-zen-nemotron", "opencode-zen-longcat"):
        snapshot = _snapshot(fleet, out={out_key})
        plan = _planned(task, snapshot, NARROW_YES)
        by_profile = {c["profile_id"]: c for c in plan["candidates"]}
        assert by_profile[f"standard-high-{out_key}"]["launchable"] is False
        other_two = {
            f"standard-high-{k}" for k in
            ("opencode-zen", "opencode-zen-nemotron", "opencode-zen-longcat")
        } - {f"standard-high-{out_key}"}
        for other in other_two:
            assert by_profile[other]["launchable"] is True


def test_narrow_hosted_policy_includes_all_three_harnesses() -> None:
    """The narrow-hosted lane names all three harnesses; they are excluded
    from the general candidate pool."""
    policy, digest = parse_policy(BALANCED_POLICY)
    assert "opencode-zen-nemotron" in policy.narrow_harnesses()
    assert "opencode-zen-longcat" in policy.narrow_harnesses()

    def plan(task, snapshot, classification):
        return plan_route(task, policy, snapshot, policy_sha256=digest,
                          classification=classification)

    # A general (non-narrow) task must not see any hosted rung as a candidate.
    general = plan(_task(task_type="bugfix"), _snapshot(_three_hosted_fleet()), NARROW_NO)
    assert general.outcome == "planned"
    assert {c["harness"] for c in general.value["candidates"]} <= {"claude", "codex"}



def test_a_fleet_of_only_local_models_names_the_gate() -> None:
    policy, digest = parse_policy(
        BALANCED_POLICY + "local_models: {harnesses: [claude, codex]}\n"
    )
    fleet = [p for p in _fleet() if p.harness in {"claude", "codex"}]
    result = plan_route(
        _task(task_type="research", blocks_work=True), policy, _snapshot(fleet),
        policy_sha256=digest,
    )
    assert result.outcome == "no_candidates"
    assert result.value["reason"] == "local_model_gate"


def test_an_allowlisted_benchmark_arm_skips_the_local_model_gate() -> None:
    # The arm names its harness explicitly, so the gate does not second-guess
    # it, even for a train bugfix.
    policy, digest = parse_policy(BALANCED_POLICY + """
benchmark_arms:
  qwen:
    class: standard-high
    harness: opencode
    requested_model: qwen3.8:27b
    observed_models: [qwen3.8*]
""")
    task = _task(task_type="bugfix", on_train=True, benchmark_arms=("qwen",))
    result = plan_route(task, policy, _snapshot(), policy_sha256=digest)
    assert result.outcome == "planned", result
    assert result.value["profile_id"] == "standard-high-opencode"


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
    assert plan["balance"]["tie_order"] == [
        "codex", "claude", "opencode", "opencode-zen",
        "opencode-zen-nemotron", "opencode-zen-longcat",
    ]
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


@pytest.mark.parametrize("change", [{"provider": "other"}, {"lifecycle": "task"}])
def test_candidate_eligibility_rejects_changed_provider_or_lifecycle(change):
    snapshot = _snapshot()
    plan = _planned(_task(task_type="research"), snapshot)
    candidate = Candidate.from_dict(plan["candidates"][0])
    assert is_candidate(candidate, snapshot)
    changed = replace(snapshot, profiles=tuple(
        replace(p, **change) if p.id == candidate.profile_id else p
        for p in snapshot.profiles
    ))
    assert not is_candidate(candidate, changed)


def test_live_context_is_bounded_and_aggregates_unknown_task_kinds():
    from src.routing.context import (
        MAX_PROFILES,
        MAX_PROVIDERS,
        MAX_QUOTA_WINDOWS,
        MAX_SUMMARY_CHARS,
        live_context,
        quota_observations,
        summarize_context,
    )

    rows = [{"window": f"window-{i}", "scope": "all models", "used_percent": i,
             "observed_at": 990, "last_seen_at": 995, "source": "/secret/path"}
            for i in range(50)]
    quota = quota_observations(rows, now=1000, stale_after=100)
    profiles = tuple(ProfileFacts(
        id=f"worker-{i}", harness="codex", provider=f"provider-{i}",
        lifecycle="task", classes=frozenset({"standard-high"}), slots=2,
    ) for i in range(100))
    snapshot = Snapshot(profiles=profiles, providers={
        p.provider: ProviderFacts(quota=quota) for p in profiles
    })
    context = live_context(
        snapshot, project_id="p", now=1000, started_at=999, supply=[],
        active_kinds={"research": 2, **{f"private-title-{i}": 1 for i in range(100)}},
        project_cap=5, project_active=True, global_cap=10, workspace_capacity=5, quarantine={},
    )
    assert len(context["profiles"]) == MAX_PROFILES
    assert len(context["providers"]) == MAX_PROVIDERS
    assert all(len(p["quota"]) == MAX_QUOTA_WINDOWS for p in context["providers"])
    assert context["truncated"]
    assert all(p["quota_truncated"] for p in context["providers"])
    assert context["active_work"] == {"research": 2, "unknown": 100}
    assert "/secret/path" not in json.dumps(context)
    assert "private-title" not in json.dumps(context)
    assert len(summarize_context(context)) <= MAX_SUMMARY_CHARS


def routine_policy():
    source = Path("src/prompts/default_playbooks/default-assignment-routing.md").read_text()
    return parse_policy(source.split("```yaml\n", 1)[1].split("```", 1)[0])


def routine_plan(snapshot=None, **task_changes):
    policy, digest = routine_policy()
    task_changes.setdefault("task_type", "feature")
    return plan_route(_task(**task_changes), policy, snapshot or _snapshot(),
                      policy_sha256=digest, classification=NARROW_NO)


@pytest.mark.parametrize("kind", ["feature", "bugfix", "refactor", "test", "docs", "chore", "sync"])
def test_routine_hosted_work_prefers_compatible_codex_despite_lower_claude_pressure(kind):
    snapshot = _snapshot(busy={"standard-high-codex": 2, "fast-high-codex": 1})
    snapshot = replace(snapshot, context={"as_of": 1000, "collection_seconds": 0.25},
                       headroom={"standard-high-codex": 1, "fast-high-codex": 1})
    result = routine_plan(snapshot, task_type=kind)
    assert result.outcome == "planned"
    assert result.value["provider"] == "codex"
    assert result.value["provider_intent"] == "class_only"
    assert result.value["decision"]["mode"] == "hosted_preference"
    assert "headroom 1" in result.value["reason"]
    assert "quota unknown" in result.value["reason"]
    assert "snapshot age 0.25s" in result.value["reason"]


@pytest.mark.parametrize("cause", ["unavailable", "busy", "backlog", "headroom", "disabled",
                                  "class", "degraded", "usage"])
def test_routine_preference_falls_back_to_claude_without_raising_class(cause):
    snapshot = _snapshot()
    if cause == "unavailable":
        snapshot = _snapshot(out={"codex"})
    elif cause in {"busy", "backlog"}:
        snapshot = _snapshot(**{cause: {"standard-high-codex": 4}})
    elif cause == "headroom":
        snapshot = replace(snapshot, headroom={"standard-high-codex": 0,
                                              "standard-high-claude": 1})
    elif cause in {"disabled", "class"}:
        changes = {"enabled": False} if cause == "disabled" else {"classes": frozenset()}
        snapshot = replace(snapshot, profiles=tuple(
            replace(p, **changes) if p.harness == "codex" else p for p in snapshot.profiles
        ))
    elif cause == "degraded":
        snapshot = _snapshot(degraded={"codex"})
    else:
        snapshot = _snapshot(usage={"codex": 95})
    result = routine_plan(snapshot)
    assert result.outcome == "planned"
    assert result.value["profile_id"] == "standard-high-claude"
    assert result.value["intelligence_class"] == "standard-high"
    assert result.value["decision"]["mode"] == "pressure_fallback"
    assert "bypassed" in result.value["reason"]


def test_routine_preference_preserves_local_design_art_and_operator_rules():
    policy, digest = routine_policy()
    narrow = plan_route(_task(task_type="bugfix"), policy, _snapshot(),
                        policy_sha256=digest, classification={**NARROW_YES, "risk": "low"})
    assert narrow.value["provider"] == "opencode"
    assert narrow.value["decision"]["mode"] == "lane_preference"
    # The shipped narrow lanes take only work classified low risk: narrow and
    # test-verified alone no longer qualifies.  No risk answer is a medium one.
    for classification in (NARROW_YES, {**NARROW_YES, "risk": "medium"}):
        hosted = plan_route(_task(task_type="bugfix"), policy, _snapshot(),
                            policy_sha256=digest, classification=classification)
        assert hosted.value["provider"] == "codex", classification
        assert {c["harness"] for c in hosted.value["candidates"]} <= {"claude", "codex"}
    assert routine_plan(task_type="design").value["profile_id"] == "deep-high-claude"
    assert routine_plan(task_type="art").value["provider_intent"] == "pinned"
    assert routine_plan(_snapshot(out={"codex"}), task_type="art").outcome == "held"
    assert routine_plan(preferred_provider="claude").value["provider"] == "claude"
    assert routine_plan(exclude_providers=frozenset({"codex"})).value["provider"] == "claude"
    assert routine_plan(_snapshot(out={"codex", "claude"})).outcome == "held"


def test_routine_preference_keeps_quota_window_identity_and_ignores_stale_or_reset_usage():
    from src.routing.context import quota_observations

    quota = quota_observations([
        {"window": "weekly", "scope": "account", "used_percent": 99, "observed_at": 100},
        {"window": "five_hour", "scope": "account", "used_percent": 20, "observed_at": 995},
        {"window": "old", "scope": "account", "used_percent": 100, "observed_at": 990,
         "resets_at": 999},
    ], now=1000, stale_after=100)
    snapshot = replace(_snapshot(busy={"standard-high-codex": 2}), providers={
        "codex": ProviderFacts(usage_percent=99, quota=quota),
        "claude": ProviderFacts(usage_percent=5),
    })
    result = routine_plan(snapshot)
    assert result.value["provider"] == "codex"
    codex = next(s for s in result.value["scores"] if s["profile_id"] == "standard-high-codex")
    assert codex["usage_percent"] == 20
    evidence = result.value["decision"]["candidates"][0]["quota"]
    assert {q["freshness"] for q in evidence} == {"fresh", "stale", "reset"}
    assert {q["window"] for q in evidence} == {"weekly", "five_hour", "old"}


def test_routine_preference_reselects_when_fresh_capacity_disappears():
    policy, _digest = routine_policy()
    plan = routine_plan().value
    candidates = [Candidate.from_dict(c) for c in plan["candidates"]]
    selected = reselect(candidates, replace(_snapshot(), headroom={
        "standard-high-codex": 0, "standard-high-claude": 1,
    }), policy.balance)
    assert selected.chosen.provider == "claude"
    assert not selected.took_hosted_preference


def test_usage_just_above_soft_limit_does_not_get_rounded_into_healthy_preference():
    result = routine_plan(_snapshot(usage={"codex": 80.00001}))
    assert result.value["decision"]["mode"] == "pressure_fallback"
    assert "own_usage_above_soft_limit" in result.value["reason"]


def test_routine_policy_keeps_allowlisted_benchmark_provider_intent():
    policy, _digest = routine_policy()
    body = policy.model_dump(mode="json", by_alias=True)
    body["benchmark_arms"] = {"claude-arm": {
        "class": "deep-high", "harness": "claude", "requested_model": "opus",
        "observed_models": ["opus"],
    }}
    policy, digest = parse_policy(json.dumps(body))
    result = plan_route(_task(task_type="feature", benchmark_arms=("claude-arm",)),
                        policy, _snapshot(), policy_sha256=digest)
    assert result.value["profile_id"] == "deep-high-claude"
    assert result.value["provider_intent"] == "pinned"


def test_origin_can_disable_hosted_preference_without_changing_class_fit():
    policy, _digest = routine_policy()
    body = policy.model_dump(mode="json", by_alias=True)
    body["origins"]["integration_repair"]["prefer_harnesses"] = []
    policy, digest = parse_policy(json.dumps(body))
    # A repair's risk could raise its class, so the plan needs the risk answer.
    result = plan_route(_task(task_type="bugfix", created_by_kind="integration_repair"),
                        policy, _snapshot(busy={"standard-high-codex": 1}), policy_sha256=digest,
                        classification={**NARROW_NO, "risk": "medium"})
    assert result.value["profile_id"] == "standard-high-claude"
    assert result.value["intelligence_class"] == "standard-high"


def test_historical_replay_quantifies_changes_and_reports_missing_evidence():
    from src.routing.replay import replay_routes

    records = []
    for i, snapshot in enumerate([
        _snapshot(busy={"standard-high-codex": 1}),
        _snapshot(busy={"standard-high-codex": 4}),
    ]):
        # The pre-change policy chooses idle Claude for both snapshots.
        route = _planned(_task(task_type="bugfix"), snapshot, NARROW_NO)
        records.append({"task_id": f"historical-{i}", "route": route})
    records.extend([
        {"task_id": "override", "route_source": "override", "route": records[0]["route"]},
        {"task_id": "missing", "route": {"provider": "claude"}},
    ])
    policy, digest = routine_policy()
    report = replay_routes(records, policy, digest)
    assert report["before"] == {"claude": 2}
    assert report["after"] == {"codex": 1, "claude": 1}
    assert report["changed"] == 1 and report["skipped"] == 2
    assert report["missing_observations"]["headroom_and_snapshot_age_unknown"] == 2
    assert "measured quota savings" in report["limitations"][1]


def test_replay_routes_a_projected_repair_on_its_routed_origin():
    from src.routing.replay import replay_routes

    policy, _digest = routine_policy()
    body = policy.model_dump(mode="json", by_alias=True)
    body["origins"]["integration_repair"]["prefer_harnesses"] = []
    policy, digest = parse_policy(json.dumps(body))
    result = plan_route(_task(task_type="bugfix", created_by_kind="integration_repair"), policy,
                        _snapshot(busy={"standard-high-codex": 1}), policy_sha256=digest,
                        classification=NARROW_NO)
    assert result.value["rule"] == "kinds.bugfix+origins.integration_repair"
    assert result.value["provider"] == "claude"
    # Stored origins the router projects to ``integration_repair``: a plain
    # bugfix rule would prefer the busy Codex cell and count a false change.
    records = [
        {"task_id": f"repair-{kind}", "created_by_kind": kind, "route": result.value}
        for kind in ("system", "source_ci_repair", "integration_writer")
    ]
    report = replay_routes(records, policy, digest)
    assert report["after"] == {"claude": 3} and report["changed"] == 0, report


# -- the per-task preference (mandatory routing §4) -------------------------------


#: A research task classified medium risk: the shipped policy's risk table
#: could raise research, so the plan needs the risk answer before it routes.
RESEARCH_MEDIUM = {**NARROW_NO, "task_type": "research", "risk": "medium"}


def _preferred_plans(snapshot=None, **task_changes):
    """``{'soft': plan, 'strict': plan}`` for the same task against one snapshot."""
    policy, digest = routine_policy()
    task_changes.setdefault("task_type", "research")
    task_changes.setdefault("prefer_target", "claude")
    return {
        mode: plan_route(
            _task(**{**task_changes, "prefer_mode": mode}),
            policy, snapshot or _snapshot(), policy_sha256=digest,
            classification=RESEARCH_MEDIUM,
        )
        for mode in ("soft", "strict")
    }


def test_a_soft_preference_takes_its_target_when_it_has_headroom():
    result = _preferred_plans(prefer_target="claude")["soft"]
    assert result.outcome == "planned"
    assert result.value["profile_id"] == "standard-high-claude"
    assert result.value["decision"]["mode"] == "task_preference"
    # Without it the shipped policy prefers Codex for routine work.
    assert routine_plan().value["profile_id"] == "standard-high-codex"


def test_a_soft_preference_falls_back_and_records_why():
    snapshot = replace(_snapshot(), headroom={"standard-high-claude": 0,
                                              "standard-high-codex": 1})
    result = _preferred_plans(snapshot, prefer_target="claude")["soft"]
    assert result.outcome == "planned"
    assert result.value["profile_id"] == "standard-high-codex"
    preference = result.value["preference"]
    assert preference == {
        "target": "claude", "mode": "soft", "kind": "harness",
        "honoured": False, "fallback_reason": "no_headroom",
    }
    assert result.value["decision"]["preference"] == preference
    assert "preferred claude (soft) not honoured (no_headroom)" in result.value["reason"]


def test_a_soft_preference_records_an_unavailable_target_and_a_provider_that_is_out():
    fleet = _snapshot()
    unknown = _preferred_plans(fleet, prefer_target="gemini")["soft"]
    assert unknown.value["preference"]["fallback_reason"] == "no_candidate_serves_the_target"
    assert unknown.value["preference"]["kind"] == "harness"
    out = _preferred_plans(_snapshot(out={"claude"}), prefer_target="claude")["soft"]
    assert out.value["preference"]["fallback_reason"] == "provider_unavailable"


def test_a_strict_preference_waits_for_its_target_and_never_falls_back():
    snapshot = replace(_snapshot(), headroom={"standard-high-claude": 0,
                                              "standard-high-codex": 1})
    result = _preferred_plans(snapshot, prefer_target="claude")["strict"]
    # Full, not busy: the task queues for its target instead of moving.
    assert result.outcome == "planned"
    assert result.value["profile_id"] == "standard-high-claude"
    assert result.value["preference"]["honoured"] is True
    assert {c["provider"] for c in result.value["candidates"]} == {"claude"}


def test_a_strict_preference_holds_while_its_provider_is_out_and_refuses_an_unknown():
    held = _preferred_plans(_snapshot(out={"claude"}), prefer_target="claude")["strict"]
    assert held.outcome == "held"
    assert held.value["preference"]["fallback_reason"] == "provider_unavailable"
    assert {c["provider"] for c in held.value["candidates"]} == {"claude"}
    refused = _preferred_plans(prefer_target="gemini")["strict"]
    assert refused.outcome == "no_candidates"
    assert refused.value["reason"] == "prefer_target_unavailable"
    assert refused.value["preference"]["fallback_reason"] == "no_candidate_serves_the_target"


def test_a_preference_may_name_one_profile_as_well_as_a_harness():
    by_profile = _preferred_plans(prefer_target="standard-high-claude")["strict"]
    assert by_profile.value["profile_id"] == "standard-high-claude"
    assert by_profile.value["preference"]["kind"] == "profile"
    # One rung of that harness only: the other rung is not a candidate.
    assert {c["profile_id"] for c in by_profile.value["candidates"]} == {"standard-high-claude"}


def test_a_preference_outranks_a_lane_preference_but_not_a_hold_lane():
    policy, digest = routine_policy()
    narrow = plan_route(
        _task(task_type="bugfix", prefer_target="claude", prefer_mode="soft"),
        policy, _snapshot(), policy_sha256=digest,
        classification={**NARROW_YES, "risk": "low"},
    )
    assert "narrow" in {c["lane"] for c in narrow.value["candidates"]}
    assert narrow.value["profile_id"] == "standard-high-claude"
    assert narrow.value["decision"]["mode"] == "task_preference"
    art = plan_route(
        _task(task_type="art", prefer_target="claude", prefer_mode="soft"),
        policy, _snapshot(), policy_sha256=digest,
    )
    # The art lane holds Codex: a soft preference does not unpick a hold.
    assert art.value["profile_id"] == "deep-high-codex"
    assert art.value["provider_intent"] == "pinned"
    assert art.value["preference"]["honoured"] is False


def test_a_benchmark_arm_pins_its_model_over_a_preference():
    policy, _digest = routine_policy()
    body = policy.model_dump(mode="json", by_alias=True)
    body["benchmark_arms"] = {"codex-arm": {
        "class": "deep-high", "harness": "codex", "requested_model": "gpt",
        "observed_models": ["gpt"],
    }}
    policy, digest = parse_policy(json.dumps(body))
    result = plan_route(
        _task(task_type="research", benchmark_arms=("codex-arm",),
              prefer_target="claude", prefer_mode="strict"),
        policy, _snapshot(), policy_sha256=digest,
    )
    assert result.outcome == "planned"
    assert result.value["profile_id"] == "deep-high-codex"
    assert result.value["preference"]["fallback_reason"] == "benchmark_arm_pins_its_model"


def test_no_preference_leaves_every_routed_decision_exactly_as_it_was():
    snapshot = _snapshot(busy={"standard-high-codex": 1})
    for changes in (
        {"task_type": "research"},
        {"task_type": "bugfix"},
        {"task_type": "design"},
        {"task_type": "art"},
        {"task_type": "chore"},
    ):
        planned = _planned(_task(**changes), snapshot, NARROW_NO)
        assert planned["preference"] is None
        assert planned["decision"]["preference"] is None
        assert planned["decision"]["mode"] in {
            "lane_preference", "hosted_preference", "pressure_fallback"
        }
        assert "preferred " not in planned["reason"].split(";")[0]
    # An explicit ``None`` and an out-of-vocabulary mode both mean "no preference".
    for task in (_task(task_type="research", prefer_target=None, prefer_mode="strict"),
                 _task(task_type="research", prefer_target="", prefer_mode="loud")):
        planned = _planned(task, snapshot)
        assert planned["preference"] is None


def test_a_preference_survives_the_candidate_round_trip_through_a_plan():
    planned = routine_plan(prefer_target="claude").value
    candidates = [Candidate.from_dict(c) for c in planned["candidates"]]
    assert any(c.prefer_target for c in candidates)
    policy, _digest = routine_policy()
    # Apply re-selects on a fresh snapshot, so the flag has to be in the plan.
    chosen = reselect(candidates, replace(_snapshot(), headroom={
        "standard-high-claude": 1, "standard-high-codex": 1,
    }), policy.balance)
    assert chosen.chosen.provider == "claude"
    assert chosen.took_preferred_target


@pytest.mark.parametrize("origin", ["integration_repair", "development_repair", "integration_verifier", "integration_writer", None])
@pytest.mark.parametrize("aliased", [False, True])
def test_active_oct03_policy_never_admits_opencode_family_to_general_pool(origin, aliased):
    policy_text = (Path(__file__).parent / "fixtures/routing/active-policy-2026-10-03.yaml").read_text()
    policy, digest = parse_policy(policy_text)
    profiles = [
        _rung("standard-high", "codex", slots=1),
        _rung("standard-high", "claude", slots=1),
        _rung("standard-high", "opencode-zen-longcat", slots=10),
        _rung("standard-high", "opencode-zen-nemotron", slots=10),
    ]
    if aliased:
        profiles.append(_rung("standard-high", "custom-cli", provider="openrouter",
                              harness_family="opencode", slots=10))
    snapshot = _snapshot(profiles, busy={"standard-high-codex": 1, "standard-high-claude": 1})
    result = plan_route(_task(task_type="bugfix", created_by_kind=origin), policy, snapshot,
                        policy_sha256=digest, classification=NARROW_NO)
    assert result.outcome == "planned", result
    result = result.value
    assert result["provider"] in {"codex", "claude"}
    assert {c["harness"] for c in result["candidates"]} <= {"codex", "claude"}


def test_active_oct03_policy_preserves_explicit_narrow_lane_admission():
    policy, digest = parse_policy((Path(__file__).parent / "fixtures/routing/active-policy-2026-10-03.yaml").read_text())
    result = plan_route(_task(task_type="bugfix"), policy,
                        _snapshot([_rung("standard-high", "opencode-zen")]),
                        policy_sha256=digest, classification=NARROW_YES)
    assert result.outcome == "planned", result
    result = result.value
    assert result["provider"] == "opencode-zen"
    assert result["candidates"][0]["lane"] == "narrow-hosted"


# -- risk ---------------------------------------------------------------------------

#: The shipped policy plus a ``risk`` block: a medium risk floors at
#: standard-high, a high one also keeps to Claude and Codex, and a very high
#: one floors at deep-high unless the change is narrow and test-verified.
RISK_POLICY = SHIPPED_POLICY + """\
risk:
  medium: {min_class: standard-high}
  high: {min_class: standard-high, harnesses: [claude, codex]}
  very_high:
    min_class: deep-high
    harnesses: [claude, codex]
    relax: {class: standard-high, requires: [narrow, test_verified]}
"""

_NARROW_CLASSES = (
    "classes: {standard-high: standard-high, fast-high: fast-low, fast-low: fast-low}\n"
)

#: The shipped policy, no ``risk`` block, with the local ``narrow`` lane
#: capped at a low risk.
LANE_CAP_POLICY = SHIPPED_POLICY.replace(
    _NARROW_CLASSES, _NARROW_CLASSES + "    max_risk: low\n",
)
assert "max_risk: low" in LANE_CAP_POLICY

#: The keys a classification record had before risks existed.
_RECORD_KEYS = {
    "task_type", "intelligence_class", "narrow", "test_verified", "independent_verifier",
    "reason",
}


def _risk_plan(text: str, task: TaskFacts, snapshot: Snapshot | None = None,
               classification=None):
    policy, digest = parse_policy(text)
    return plan_route(task, policy, snapshot or _snapshot(), policy_sha256=digest,
                      classification=classification)


def test_a_policy_without_risk_keys_keeps_its_digest() -> None:
    # An optional key at its default is left out of the canonical JSON, so
    # no existing policy's digest moves when one is added.  SHIPPED_POLICY is
    # the default routing playbook's policy before the risk-aware revision;
    # the shipped playbook now uses the risk keys.  Re-pinned once when
    # ``local_models`` (always dumped) lost its priority ceiling: priority no
    # longer routes (rev-wise-impact), so the old digest named a removed rule.
    assert DIGEST == "sha256:b9c8debd9e70dffbcf9ca3fac05e831c7be43be969e8f7bc1cb61da7ca9d70e2"
    assert parse_policy(SHIPPED_POLICY)[1] == (
        "sha256:4f01d917e1c24fbd7abb823f7f7e40b5b333ad7620194a3972a3bb8da3872f6b"
    )
    fixture = (Path(__file__).parent / "fixtures/routing/active-policy-2026-10-03.yaml")
    assert parse_policy(fixture.read_text())[1] == (
        "sha256:a10459c0fe546c30e791a8ae15afa80fa4a1ce8f15f4971e6f678c36221d3cb1"
    )
    dumped = POLICY.model_dump(mode="json", by_alias=True)
    assert "risk" not in dumped
    assert not any("max_risk" in lane for lane in dumped["lanes"].values())
    assert not POLICY.uses_risk

    # A policy that uses them digests them.
    risky, risky_digest = parse_policy(RISK_POLICY)
    assert risky_digest != parse_policy(SHIPPED_POLICY)[1]
    assert set(risky.model_dump(mode="json", by_alias=True)["risk"]) == {
        "medium", "high", "very_high",
    }
    capped, capped_digest = parse_policy(LANE_CAP_POLICY)
    assert capped_digest != parse_policy(SHIPPED_POLICY)[1]
    lane = capped.model_dump(mode="json", by_alias=True)["lanes"]["narrow"]
    assert lane["max_risk"] == "low"


def test_a_risk_block_parses() -> None:
    assert RISK_LEVELS == ("low", "medium", "high", "very_high")
    policy, _digest = parse_policy(RISK_POLICY)
    assert policy.uses_risk
    very_high = policy.risk["very_high"]
    assert (very_high.min_class, very_high.harnesses) == ("deep-high", ("claude", "codex"))
    assert very_high.relax.class_ == "standard-high"
    assert very_high.floor({"narrow", "test_verified"}) == "standard-high"
    assert very_high.floor({"narrow"}) == "deep-high"
    assert policy.risk["medium"].harnesses == ()

    capped, _digest = parse_policy(LANE_CAP_POLICY)
    assert capped.uses_risk and not capped.risk
    assert capped.lanes["narrow"].max_risk == "low"


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        (SHIPPED_POLICY + "risk:\n  extreme: {min_class: deep-high}\n", "not a risk level"),
        (SHIPPED_POLICY + "risk:\n  high: {harnesses: [claude]}\n", "risk.high.min_class"),
        (SHIPPED_POLICY + "risk:\n  high: {min_class: galaxy}\n", "galaxy"),
        (SHIPPED_POLICY + "risk:\n  high: {min_class: deep-high, harnesses: ['*']}\n",
         "harness selectors"),
        (SHIPPED_POLICY + "risk:\n  high:\n    min_class: standard-high\n"
         "    relax: {class: deep-high, requires: [narrow]}\n",
         "relax.class ranks above min_class"),
        (SHIPPED_POLICY + "risk:\n  high:\n    min_class: deep-high\n"
         "    relax: {class: standard-high, requires: []}\n", "risk.high.relax.requires"),
        (SHIPPED_POLICY + "risk:\n  high:\n    min_class: deep-high\n"
         "    relax: {class: standard-high, requires: [vibes]}\n", "vibes"),
        (SHIPPED_POLICY.replace("hold: true}", "hold: true, max_risk: low}"),
         "'max_risk' belongs to a narrow lane"),
        (LANE_CAP_POLICY.replace("max_risk: low", "max_risk: extreme"), "not a risk level"),
        # Priority no longer routes; a policy still writing the old ceiling
        # is refused by name rather than silently ignored.
        (LANE_CAP_POLICY.replace("max_risk: low\n", "max_risk: low\n    below_priority: 100\n"),
         "'below_priority' is retired"),
        (SHIPPED_POLICY + "local_models: {below_priority: 150}\n", "'below_priority' is retired"),
    ],
    ids=[
        "unknown-level", "no-min-class", "unknown-class", "bad-selector", "relax-above-floor",
        "relax-no-flags", "relax-unknown-flag", "max-risk-design-lane",
        "unknown-max-risk", "lane-below-priority-retired", "local-below-priority-retired",
    ],
)
def test_an_invalid_risk_key_is_refused(text: str, fragment: str) -> None:
    with pytest.raises(PolicyError, match=fragment):
        parse_policy(text)


def test_read_classification_reads_an_optional_risk() -> None:
    policy, _digest = parse_policy(RISK_POLICY)
    classified, failed, error = read_classification(
        {**NARROW_YES, "risk": "high", "risk_reason": "touches the scheduler"}, policy,
    )
    assert (failed, error) == (False, None)
    assert (classified.risk, classified.risk_reason) == ("high", "touches the scheduler")
    record = classified.as_dict()
    assert (record["risk"], record["risk_reason"]) == ("high", "touches the scheduler")
    assert read_classification(record, policy)[0] == classified  # replay reads it back

    # Absent or null: unknown, and the record is the one it was before risks.
    for raw in (NARROW_YES, {**NARROW_YES, "risk": None}):
        classified, failed, _error = read_classification(raw, policy)
        assert not failed and classified.risk is None
        assert set(classified.as_dict()) == _RECORD_KEYS

    long = read_classification({**NARROW_YES, "risk": "low", "risk_reason": "x" * 1000}, policy)
    assert len(long[0].risk_reason) == 400


@pytest.mark.parametrize("risk", ["extreme", "HIGH", 3, ["low"]])
def test_an_unknown_risk_fails_the_classification(risk) -> None:
    policy, _digest = parse_policy(RISK_POLICY)
    classified, failed, error = read_classification({**NARROW_YES, "risk": risk}, policy)
    assert classified is None and failed
    assert error == f"classification risk {risk!r} is not a risk level"


def test_a_non_string_risk_reason_fails_the_classification() -> None:
    policy, _digest = parse_policy(RISK_POLICY)
    classified, failed, error = read_classification(
        {**NARROW_YES, "risk": "low", "risk_reason": 7}, policy,
    )
    assert classified is None and failed and "risk_reason" in error


def test_a_medium_risk_raises_a_hinted_fast_high_task_to_standard_high() -> None:
    task = _task(task_type="chore", class_hint="fast-high")
    classification = {
        **NARROW_NO, "task_type": "chore", "intelligence_class": "fast-high", "risk": "medium",
    }
    plan = _risk_plan(RISK_POLICY, task, classification=classification)
    assert plan.outcome == "planned", plan
    plan = plan.value
    assert plan["intelligence_class"] == "standard-high"
    assert plan["class_raised_for_risk"] == {
        "from": "fast-high", "to": "standard-high", "risk": "medium",
    }
    assert "class raised from fast-high to standard-high for risk medium" in plan["reason"]
    assert plan["classification"]["risk"] == "medium"

    # A low risk has no rule: the hint stands and the plan carries no raise.
    low = _risk_plan(RISK_POLICY, task, classification={**classification, "risk": "low"})
    assert low.value["intelligence_class"] == "fast-high"
    assert "class_raised_for_risk" not in low.value
    # Nor does a plan under a policy without risks, nor its classification record.
    plain = _risk_plan(SHIPPED_POLICY, task, classification={
        **NARROW_NO, "task_type": "chore", "intelligence_class": "fast-high",
    })
    assert "class_raised_for_risk" not in plain.value
    assert set(plain.value["classification"]) == _RECORD_KEYS


@pytest.mark.parametrize(
    ("flags", "floor"),
    [
        ({"narrow": True, "test_verified": True}, "standard-high"),
        ({"narrow": True, "test_verified": False}, "deep-high"),
    ],
    ids=["relaxed", "not-relaxed"],
)
def test_a_very_high_risk_floors_at_deep_high_unless_narrow_and_tested(flags, floor) -> None:
    task = _task(task_type="chore", class_hint="fast-high")
    classification = {
        **NARROW_NO, **flags, "task_type": "chore", "intelligence_class": "fast-high",
        "risk": "very_high",
    }
    plan = _risk_plan(RISK_POLICY, task, classification=classification)
    assert plan.outcome == "planned", plan
    # The floor beats the hint and the chore's standard-high max_class alike.
    assert plan.value["intelligence_class"] == floor
    assert plan.value["class_raised_for_risk"] == {
        "from": "fast-high", "to": floor, "risk": "very_high",
    }
    # Claude and Codex only: even a narrow, tested change skips OpenCode.
    assert {c["harness"] for c in plan.value["candidates"]} <= {"claude", "codex"}


def test_a_high_risk_keeps_only_claude_and_codex() -> None:
    fleet = (*_hosted_fleet(), _rung("standard-high", "gemini", slots=4))
    task = _task(task_type="bugfix")
    low = _risk_plan(RISK_POLICY, task, _snapshot(fleet), {**NARROW_YES, "risk": "low"})
    assert {"opencode", "opencode-zen", "gemini"} <= {
        c["harness"] for c in low.value["candidates"]
    }
    high = _risk_plan(RISK_POLICY, task, _snapshot(fleet), {**NARROW_YES, "risk": "high"})
    assert high.outcome == "planned", high
    assert {c["harness"] for c in high.value["candidates"]} == {"claude", "codex"}

    # Nothing else installed: the emptied list names the stage.
    alone = _risk_plan(RISK_POLICY, task, _snapshot([_rung("standard-high", "gemini")]),
                       {**NARROW_YES, "risk": "high"})
    assert (alone.outcome, alone.value["reason"]) == ("no_candidates", "risk_harnesses")


def test_a_missing_risk_answer_is_treated_as_medium() -> None:
    # An answer without a risk gets the medium rule's floor, and the plan
    # says the risk was assumed rather than answered.
    task = _task(task_type="chore", class_hint="fast-high")
    classification = {**NARROW_NO, "task_type": "chore", "intelligence_class": "fast-high"}
    plan = _risk_plan(RISK_POLICY, task, classification=classification)
    assert plan.outcome == "planned", plan
    assert plan.value["intelligence_class"] == "standard-high"
    assert plan.value["class_raised_for_risk"] == {
        "from": "fast-high", "to": "standard-high", "risk": "medium", "assumed": True,
    }
    assert "no risk answer, treated as medium" in plan.value["reason"]
    # The record keeps what the classifier said, which named no risk.
    assert set(plan.value["classification"]) == _RECORD_KEYS

    # A failed classification answered nothing, the risk included.
    failed = _risk_plan(RISK_POLICY, task, classification={**classification, "risk": "extreme"})
    assert failed.outcome == "planned", failed
    assert failed.value["classification"]["failed"] is True
    assert failed.value["intelligence_class"] == "standard-high"
    assert failed.value["class_raised_for_risk"]["assumed"] is True

    # An answered risk is not assumed.
    answered = _risk_plan(RISK_POLICY, task, classification={**classification, "risk": "medium"})
    assert "assumed" not in answered.value["class_raised_for_risk"]


def test_an_assumed_medium_takes_the_medium_harnesses_and_meets_a_medium_cap() -> None:
    policy = LANE_CAP_POLICY.replace("max_risk: low", "max_risk: medium") + (
        "risk:\n  medium: {min_class: standard-high, harnesses: [claude, codex, opencode]}\n"
    )
    plan = _risk_plan(policy, _task(task_type="bugfix"), classification=NARROW_YES)
    assert plan.outcome == "planned", plan
    assert {c["harness"] for c in plan.value["candidates"]} <= {"claude", "codex", "opencode"}
    assert plan.value["profile_id"] == "standard-high-opencode"
    assert "no risk answer, treated as medium" in plan.value["reason"]


def test_no_risk_is_assumed_before_the_classifier_answers() -> None:
    # The question is still asked: an assumed medium would hide the lanes a
    # low answer opens.
    asked = _risk_plan(LANE_CAP_POLICY, _task(task_type="bugfix"))
    assert asked.outcome == "needs_classification", asked
    assert "risk" in asked.value["questions"]


def test_a_policy_that_reads_no_risk_assumes_none() -> None:
    task = _task(task_type="chore", class_hint="fast-high")
    plain = _risk_plan(SHIPPED_POLICY, task, classification={
        **NARROW_NO, "task_type": "chore", "intelligence_class": "fast-high",
    })
    assert plain.value["intelligence_class"] == "fast-high"
    assert "treated as medium" not in plain.value["reason"]


@pytest.mark.parametrize(
    ("risk", "admitted"),
    [("low", True), ("medium", False), ("very_high", False), (None, False)],
)
def test_a_narrow_lane_admits_work_up_to_its_max_risk(risk, admitted) -> None:
    # No risk answer is treated as medium, which a low cap refuses.
    classification = NARROW_YES if risk is None else {**NARROW_YES, "risk": risk}
    plan = _risk_plan(LANE_CAP_POLICY, _task(task_type="bugfix"), classification=classification)
    assert plan.outcome == "planned", plan
    assert (plan.value["profile_id"] == "standard-high-opencode") is admitted
    assert ("narrow" in {c["lane"] for c in plan.value["candidates"]}) is admitted


def test_a_risk_rule_that_could_change_the_route_asks_for_the_risk() -> None:
    # A typed, hinted task plans without a classifier call under a policy
    # without risks...
    task = _task(task_type="research", class_hint="standard-high")
    assert _risk_plan(SHIPPED_POLICY, task).outcome == "planned"
    # ...and asks once a risk floor could raise it: very_high floors at
    # deep-high, and its relax reads two flags.
    asked = _risk_plan(RISK_POLICY, task)
    assert asked.outcome == "needs_classification", asked
    assert asked.value["questions"] == ["narrow", "risk", "test_verified"]

    # Already at the top floor, with every candidate on Claude or Codex: no
    # risk answer could change the route.
    top = _risk_plan(RISK_POLICY, _task(task_type="research", class_hint="deep-high"))
    assert top.outcome == "planned", top

    # A harness restriction alone asks when it would remove a candidate.
    codex_only = SHIPPED_POLICY + "risk:\n  high: {min_class: fast-off, harnesses: [codex]}\n"
    asked = _risk_plan(codex_only, task)
    assert asked.outcome == "needs_classification", asked
    assert asked.value["questions"] == ["risk"]


def test_a_narrow_lane_with_max_risk_asks_for_the_risk() -> None:
    task = _task(task_type="bugfix")
    assert _risk_plan(LANE_CAP_POLICY, task).value["questions"] == [
        "narrow", "risk", "test_verified",
    ]
    # Without risk keys the questions are the ones asked before risks existed.
    assert _risk_plan(SHIPPED_POLICY, task).value["questions"] == ["narrow", "test_verified"]
