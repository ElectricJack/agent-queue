"""Pure policy composition: proposals, promoted omission and mandatory invariants."""

from __future__ import annotations

import itertools
import random
from dataclasses import fields, replace

import pytest

from src.test_selection import reasons as r
from src.test_selection.catalogue import Area, load_catalogue, load_policy, load_rules
from src.test_selection.mandatory import MandatoryResult, mandatory_set
from src.test_selection.policy import PromotionIdentity, compose, omit_candidate, order_modules
from src.test_selection.snapshot import ChangedPath, ChangeSnapshot
from src.test_selection.static_impact import StaticResult
from src.test_selection.typesafe import AreaAnswer, JevResult
from tests.selection_fixture_repo import build_fixture_repo

MODES = ("plan_only", "shadow", "enforce")
A = "tests/test_a.py"
B = "tests/sub/test_b.py"
C = "tests/test_c.py"
D = "tests/test_docs_scan.py"


@pytest.fixture(autouse=True)
def _pg_backend():
    """This module uses only pure functions and a disposable git fixture."""


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    repo = build_fixture_repo(tmp_path_factory.mktemp("policy") / "repo")
    catalogue = load_catalogue(repo / "tests/selection_catalogue.json")
    rules = load_rules(repo / "tests/selection_rules.yaml", catalogue)
    policy = load_policy(repo / "tests/selection_policy.yaml")
    return catalogue, rules, policy


def mandatory(world, path="src/pkg/a.py", *, complete=True):
    catalogue, rules, _ = world
    snapshot = ChangeSnapshot(
        "/w",
        "origin/main",
        "b" * 40,
        "a" * 40,
        (ChangedPath(path, "modified"),),
        "d",
        complete,
        None if complete else "unknown_base",
        None,
        0.0,
    )
    return mandatory_set(snapshot, catalogue, rules)


def static(*modules, complete=True, reason=None):
    return StaticResult(frozenset(modules), complete, "fixed", reason, 0, 1)


def answer(area, choice="unaffected", *, probability=0.985, confidence=0.95):
    probabilities = {
        "affected": (1 - probability) / 2,
        "unaffected": probability,
        "unknown": (1 - probability) / 2,
    }
    return AreaAnswer(area, choice, probabilities, confidence)


def jev(*answers, status="ok", reason=None):
    return JevResult(
        status,
        {a.area_id: a for a in answers},
        "jev-1.13.0",
        "jev-1.13.0",
        reason,
        10,
        2,
        1,
        5,
    )


def everything_no(world):
    return jev(*(answer(area) for area in world[0].areas))


def selection(world, **kwargs):
    return compose(
        catalogue=world[0],
        mandatory=kwargs.pop("mandatory", mandatory(world)),
        static=kwargs.pop("static", static(A, B)),
        jev=kwargs.pop("jev", everything_no(world)),
        policy=world[2],
        mode=kwargs.pop("mode", "enforce"),
        promoted=kwargs.pop("promoted", True),
        evidence_complete=kwargs.pop("evidence_complete", True),
        **kwargs,
    )


@pytest.mark.parametrize(
    "choice,probability,confidence,expected",
    [
        ("unaffected", 0.98, 0.90, True),
        ("unaffected", 0.985, 0.95, True),
        ("unaffected", 0.979999, 0.95, False),
        ("unaffected", 0.985, 0.899999, False),
        ("unknown", 1.0, 1.0, False),
        ("affected", 1.0, 1.0, False),
    ],
)
def test_omission_predicate(world, choice, probability, confidence, expected):
    assert (
        omit_candidate(
            answer("alpha", choice, probability=probability, confidence=confidence), world[2]
        )
        is expected
    )
    assert not omit_candidate(AreaAnswer("alpha", "unaffected", {}, 1.0), world[2])


@pytest.mark.parametrize(
    "mode,promoted,complete", itertools.product(MODES, (False, True), (False, True))
)
def test_omission_requires_promotion_and_complete_evidence(world, mode, promoted, complete):
    result = selection(world, mode=mode, promoted=promoted, evidence_complete=complete)
    allowed = mode == "shadow" or (promoted and complete)
    assert result.universe == world[0].universe
    assert result.mandatory == {A}
    assert result.static == {A, B}
    assert result.fallback == {A, B}
    assert result.jev == set()
    assert result.final == ({A} if allowed else {A, B})
    assert result.jev_used_for_omission is (mode != "shadow" and promoted and complete)
    assert result.evidence_complete is complete
    expected = None if allowed else ("not_promoted" if not promoted else "evidence_incomplete")
    assert result.fallback_reason == expected
    assert result.area_decisions["alpha"].omitted


def test_shadow_proposal_and_unpromoted_enforcement(world):
    answers = jev(
        answer("alpha", "affected", probability=0.05),
        answer("beta"),
        answer("gamma", "unknown", probability=0.2),
        answer("docs-guards"),
    )
    proposal = selection(world, jev=answers, mode="shadow", promoted=False)
    assert proposal.jev == proposal.final == {A, C}
    assert proposal.fallback == {A, B}
    assert proposal.area_decisions["beta"].reason == r.JEV_CONFIDENT_UNAFFECTED
    assert proposal.area_decisions["gamma"].reason == r.JEV_UNKNOWN
    assert not proposal.jev_used_for_omission
    enforce = selection(world, jev=answers, promoted=False)
    assert enforce.final == {A, B, C}
    assert enforce.fallback_reason == "not_promoted"


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "status,reason",
    [
        ("partial", r.JEV_MISSING_ANSWER),
        ("timeout", r.FALLBACK_TIMEOUT),
        ("unavailable", r.FALLBACK_HTTP_429),
        ("invalid", r.JEV_INVALID),
        ("over_budget", r.FALLBACK_BUDGET),
        ("model_drift", r.JEV_MODEL_DRIFT),
        ("disabled", r.JEV_DISABLED),
        ("unconfigured", r.JEV_UNCONFIGURED),
    ],
)
def test_non_ok_results_never_use_partial_answers(world, mode, status, reason):
    result = selection(world, mode=mode, jev=jev(answer("alpha"), status=status, reason=reason))
    assert result.jev is None
    assert result.final == result.fallback == {A, B}
    assert result.fallback_reason == reason
    assert not result.jev_used_for_omission
    assert result.area_decisions == {}


def test_disabled_jev_uses_fallback(world):
    result = selection(world, jev=None)
    assert result.jev is None and result.final == result.fallback
    assert result.fallback_reason == r.JEV_DISABLED


def test_missing_answers_include_their_areas(world):
    result = selection(world, jev=jev(answer("alpha")))
    assert result.jev == {B, C, D}
    assert result.final == world[0].universe  # alpha remains mandatory
    decision = result.area_decisions["beta"]
    assert decision.reason == r.JEV_MISSING_ANSWER and not decision.omitted
    assert (
        decision.choice
        is decision.p_affected
        is decision.p_unaffected
        is decision.confidence
        is None
    )


@pytest.mark.parametrize(
    "choice,probability,confidence,reason",
    [
        ("unknown", 0.01, 1.0, r.JEV_UNKNOWN),
        ("affected", 0.01, 1.0, r.JEV_AFFECTED),
        ("unaffected", 0.979, 1.0, r.JEV_AFFECTED),
        ("unaffected", 0.99, 0.89, r.JEV_AFFECTED),
    ],
)
def test_uncertainty_keeps_the_whole_universe(world, choice, probability, confidence, reason):
    answers = jev(
        *(answer(a, choice, probability=probability, confidence=confidence) for a in world[0].areas)
    )
    result = selection(world, jev=answers)
    assert result.jev == result.final == world[0].universe
    assert all(d.reason == reason and not d.omitted for d in result.area_decisions.values())
    assert result.area_decisions["beta"].choice == choice
    assert result.area_decisions["beta"].p_unaffected == probability
    assert result.area_decisions["beta"].confidence == confidence


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("kind", ("global", "snapshot", "static"))
def test_full_fallbacks_cannot_be_narrowed(world, mode, kind):
    m = mandatory(
        world, "pyproject.toml" if kind == "global" else "src/pkg/a.py", complete=kind != "snapshot"
    )
    s = static(
        B, complete=kind != "static", reason=r.STATIC_UNAVAILABLE if kind == "static" else None
    )
    result = selection(world, mandatory=m, static=s, mode=mode)
    assert result.full_required
    assert result.mandatory == result.fallback == result.final == world[0].universe
    assert result.static == set() and result.jev is None
    assert not result.jev_used_for_omission
    if kind == "static":
        assert result.fallback_reason == r.STATIC_UNAVAILABLE
        assert all(r.STATIC_UNAVAILABLE in codes for codes in result.reasons.values())
    else:
        assert result.reasons[A] == m.reasons[A]


def test_reasons_merge_mandatory_static_and_included_areas(world):
    result = selection(world, jev=jev(answer("alpha", "affected"), answer("beta")))
    assert result.reasons[A] == (*mandatory(world).reasons[A], r.STATIC_IMPACT, r.JEV_AFFECTED)
    assert result.reasons[B] == (r.STATIC_IMPACT,)
    assert result.reasons[C] == (r.JEV_MISSING_ANSWER,)
    assert r.JEV_CONFIDENT_UNAFFECTED not in result.reasons[B]


def test_mandatory_set_is_preserved_property(world):
    rng = random.Random(7)
    catalogue = world[0]
    for _ in range(200):
        modules = frozenset(m for m in catalogue.universe if rng.choice((False, True)))
        m = MandatoryResult(
            modules, False, {p: (r.EXPLICIT_TARGET,) for p in modules}, (), frozenset(), ()
        )
        s = static(*(p for p in catalogue.universe if rng.choice((False, True))))
        answers = []
        for area in catalogue.areas:
            if rng.random() < 0.2:
                continue
            weights = [rng.random() for _ in range(3)]
            probabilities = dict(
                zip(("affected", "unaffected", "unknown"), (w / sum(weights) for w in weights))
            )
            answers.append(
                AreaAnswer(area, rng.choice(tuple(probabilities)), probabilities, rng.random())
            )
        j = jev(*answers, status=rng.choice(("ok", "partial", "timeout")))
        for mode, promoted, complete in itertools.product(MODES, (False, True), (False, True)):
            result = selection(
                world,
                mandatory=m,
                static=s,
                jev=j,
                mode=mode,
                promoted=promoted,
                evidence_complete=complete,
            )
            assert modules <= result.final <= catalogue.universe
            assert frozenset(result.ordered) == result.final


def test_ordering_uses_probability_then_duration_then_path(world):
    answers = jev(
        AreaAnswer("alpha", "affected", {"affected": 0.6, "unaffected": 0.3, "unknown": 0.1}, 0.5),
        AreaAnswer(
            "gamma", "affected", {"affected": 0.9, "unaffected": 0.05, "unknown": 0.05}, 0.9
        ),
        AreaAnswer("beta", "unknown", {"affected": 0.2, "unaffected": 0.2, "unknown": 0.6}, 0.9),
        answer("docs-guards"),
    )
    result = selection(world, jev=answers, mode="shadow", durations={B: 30.0})
    assert result.ordered == (C, A, B)
    assert frozenset(result.ordered) == result.final
    assert order_modules([B, A, B], catalogue=world[0], area_decisions={}, durations={B: 10}) == (
        B,
        A,
    )
    assert order_modules([B, A], catalogue=world[0], area_decisions={}, durations=None) == (B, A)


def test_overlapping_areas_use_union_and_max_probability(world):
    catalogue = world[0]
    overlap = Area("overlap", "Shared alpha and beta tests", (A, B))
    catalogue = replace(
        catalogue,
        areas={**catalogue.areas, "overlap": overlap},
        modules={
            **catalogue.modules,
            A: replace(catalogue.modules[A], areas=("alpha", "overlap")),
            B: replace(catalogue.modules[B], areas=("beta", "overlap")),
        },
    )
    altered = (catalogue, world[1], world[2])
    answers = jev(
        *(answer(a) for a in world[0].areas),
        AreaAnswer(
            "overlap", "affected", {"affected": 0.9, "unaffected": 0.05, "unknown": 0.05}, 0.95
        ),
    )
    result = selection(altered, jev=answers, durations={B: 10.0})
    assert result.jev == result.final == {A, B}
    assert result.ordered == (B, A)
    assert result.reasons[B] == (r.STATIC_IMPACT, r.JEV_AFFECTED)


@pytest.mark.parametrize("field", fields(PromotionIdentity))
def test_promotion_identity_matches_every_field(field):
    identity = PromotionIdentity("jev-1.13.0", 1, "sha256:c", "sha256:r", "sha256:p")
    assert identity.matches(replace(identity))
    value = 2 if field.name == "question_schema_version" else "changed"
    assert not identity.matches(replace(identity, **{field.name: value}))


def test_invalid_input_membership_is_a_bug_not_silent_fallback(world):
    with pytest.raises(AssertionError):
        selection(world, static=static("tests/uncatalogued.py"), promoted=False)
