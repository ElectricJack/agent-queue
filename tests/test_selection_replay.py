"""Historical selection is blind to labels; only usable held-out reds earn recall."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.config import TestSelectionConfig as SelectionConfig
from src.git.manager import GitManager
from src.test_selection.catalogue import load_catalogue
from src.test_selection.replay import ReplayCase, ReplayOutcome, replay_case, score, wilson
from src.test_selection.service import SelectionService
from src.test_selection.static_impact import FixedStaticImpact
from src.test_selection.typesafe import FakeTransport, TransportResponse
from tests.selection_fixture_repo import build_fixture_repo, git


@pytest.fixture
def history(tmp_path):
    repo = build_fixture_repo(tmp_path / "repo")
    base = git(repo, "rev-parse", "HEAD").strip()
    commits = []
    for path, content in (
        ("src/pkg/a.py", "def alpha():\n    return 99\n"),
        ("src/pkg/c.py", "def gamma():\n    return 99\n"),
        ("pyproject.toml", '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n# change\n'),
    ):
        (repo / path).write_text(content)
        git(repo, "add", path)
        git(repo, "commit", "-qm", f"red {path}")
        commits.append(git(repo, "rev-parse", "HEAD").strip())
    return repo, base, commits


def case(history, index=0, **kwargs):
    _, base, commits = history
    module = "tests/test_c.py" if index == 1 else "tests/test_a.py"
    node = module + ("::test_gamma" if index == 1 else "::test_alpha")
    return replace(
        ReplayCase(
            commits[index], base, frozenset({module}), frozenset({node}), float(index + 10), "ci"
        ),
        **kwargs,
    )


def selector(*, unaffected=False):
    answers = {}
    for area in ("alpha", "beta", "gamma", "docs_guards"):
        choice = "unaffected" if unaffected else "affected"
        answers[f"area_{area}"] = {
            "type": "choice",
            "choice": choice,
            "confidence": 1.0,
            "probabilities": {
                key: float(key == choice) for key in ("affected", "unaffected", "unknown")
            },
        }
    transport = FakeTransport(TransportResponse("jev-1.13.0", answers, 10, 1))

    async def default_branch(_project_id):
        return "main"

    return SelectionService(
        db=None,
        git=GitManager(),
        config_getter=lambda: SelectionConfig(enabled=True, jev_enabled=True),
        static=FixedStaticImpact({"tests/test_a.py", "tests/sub/test_b.py"}),
        transport_factory=lambda _config: transport,
        default_branch_getter=default_branch,
    )


async def test_labels_are_inaccessible_until_offline_selection(history, tmp_path, monkeypatch):
    repo, _, _ = history
    original = case(history)
    selected = False

    class BlindCase:
        def __getattr__(self, name):
            if name not in {"commit", "base"}:
                assert selected, f"label leaked before selection: {name}"
            return getattr(original, name)

    def offline(workspace, *, base_ref, targets):
        nonlocal selected
        workspace = Path(workspace)
        assert not selected and targets == () and base_ref == original.base
        assert git(repo, "rev-parse", "HEAD").strip() == history[2][-1]
        assert git(workspace, "rev-parse", "HEAD").strip() == original.commit
        catalogue = load_catalogue(workspace / "tests/selection_catalogue.json")
        selected = True
        return {
            "fallback_modules": ["tests/test_a.py"],
            "mandatory_modules": ["tests/test_a.py"],
            "jev_modules": None,
            "jev_status": "disabled",
            "full_required": False,
            "catalogue_digest": catalogue.digest,
            "snapshot_complete": True,
            "base_sha": original.base,
            "head_sha": original.commit,
        }

    monkeypatch.setattr("src.test_selection.replay.select_offline", offline)
    outcome = await replay_case(
        GitManager(), str(repo), BlindCase(), service=None, workdir=tmp_path / "replays"
    )
    assert outcome.covered["fallback"] and not outcome.evidence_missing
    assert not outcome.anachronistic
    assert list((tmp_path / "replays").iterdir()) == []
    assert len(git(repo, "worktree", "list", "--porcelain").split("worktree ")) == 2


@pytest.mark.parametrize("unaffected", [False, True])
async def test_fake_jev_preserves_mandatory_and_scores_jev_alone(history, tmp_path, unaffected):
    repo, _, _ = history
    outcome = await replay_case(
        GitManager(),
        str(repo),
        case(history),
        service=selector(unaffected=unaffected),
        workdir=tmp_path / "runs",
        existing_worker_selection=frozenset(),
    )
    assert outcome.covered["fallback"] and outcome.covered["mandatory_plus_jev"]
    assert outcome.covered["jev_alone"] is (not unaffected)
    assert not outcome.covered["existing_worker"]
    assert not outcome.critical_miss  # analytical J and worker misses cannot revoke M ∪ J
    report = score([outcome], split_at=0)
    assert not report.promotion_bar_met
    if unaffected:
        assert sum(b["failures"] for b in report.calibration) == 1


async def test_baseline_exact_nodes_hub_and_absent_evidence(history, tmp_path):
    repo, _, _ = history
    second = case(history, 1)
    baseline = await replay_case(
        GitManager(),
        str(repo),
        replace(second, known_failures=second.failing_node_ids),
        service=selector(),
        workdir=tmp_path / "runs",
    )
    assert not baseline.evidence_missing and not baseline.failing_modules
    # Exclude a known node, never its entire module or a substring match.
    mixed = replace(
        second,
        failing_node_ids=second.failing_node_ids | {"tests/test_c.py::new"},
        known_failures=second.failing_node_ids,
    )
    remaining = await replay_case(
        GitManager(), str(repo), mixed, service=selector(), workdir=tmp_path / "runs"
    )
    assert remaining.failing_modules == frozenset({"tests/test_c.py"})
    missing = await replay_case(
        GitManager(),
        str(repo),
        case(history, failing_node_ids=frozenset()),
        service=selector(),
        workdir=tmp_path / "runs",
    )
    assert missing.evidence_missing
    hub = await replay_case(
        GitManager(), str(repo), case(history, 2), service=selector(), workdir=tmp_path / "runs"
    )
    assert hub.full_required and hub.covered["mandatory_plus_jev"]
    # Different label variants of one red commit must not count twice.
    report = score([baseline, missing, hub], split_at=11)
    assert (report.cases, report.usable, report.tune, report.held_out) == (3, 1, 0, 1)
    report = score([remaining, hub], split_at=11)
    assert report.recall["mandatory_plus_jev"]["hub"]["usable"] == 1
    assert report.recall["mandatory_plus_jev"]["non_hub"]["usable"] == 1


@pytest.mark.parametrize("flag", ["artifact_expired", "ambiguous_cause"])
async def test_untrustworthy_labels_are_missing_evidence(history, tmp_path, flag):
    outcome = await replay_case(
        GitManager(),
        str(history[0]),
        case(history, **{flag: True}),
        service=selector(),
        workdir=tmp_path / "runs",
    )
    assert outcome.evidence_missing and score([outcome], split_at=0).usable == 0


async def test_anachronistic_catalogue_never_usable(history, tmp_path, monkeypatch):
    async def changed_selection(self, request, *, persist):
        assert not persist
        return {
            "fallback_modules": ["tests/test_a.py"],
            "mandatory_modules": [],
            "jev_modules": ["tests/test_a.py"],
            "jev_status": "ok",
            "catalogue_digest": "sha256:newer",
            "full_required": False,
            "snapshot_complete": True,
            "base_sha": request.base_ref,
            "head_sha": git(Path(request.workspace), "rev-parse", "HEAD").strip(),
        }

    monkeypatch.setattr(SelectionService, "select", changed_selection)
    outcome = await replay_case(
        GitManager(), str(history[0]), case(history), service=selector(), workdir=tmp_path / "runs"
    )
    assert outcome.anachronistic and score([outcome], split_at=0).usable == 0


async def test_cleanup_after_selection_error(history, tmp_path, monkeypatch):
    async def broken(self, request, *, persist):
        raise RuntimeError("selection failed")

    monkeypatch.setattr(SelectionService, "select", broken)
    with pytest.raises(RuntimeError, match="selection failed"):
        await replay_case(
            GitManager(),
            str(history[0]),
            case(history),
            service=selector(),
            workdir=tmp_path / "runs",
        )
    assert list((tmp_path / "runs").iterdir()) == []


async def test_cleanup_after_selection_cancellation(history, tmp_path, monkeypatch):
    import asyncio

    started = asyncio.Event()

    async def hanging(self, request, *, persist):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(SelectionService, "select", hanging)
    task = asyncio.create_task(
        replay_case(
            GitManager(),
            str(history[0]),
            case(history),
            service=selector(),
            workdir=tmp_path / "runs",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list((tmp_path / "runs").iterdir()) == []


async def test_critical_miss_is_derived_from_historical_rules(history, tmp_path, monkeypatch):
    async def broken_candidate(self, request, *, persist):
        catalogue = load_catalogue(Path(request.workspace) / "tests/selection_catalogue.json")
        return {
            "fallback_modules": ["tests/test_a.py"],
            "mandatory_modules": [],
            "jev_modules": [],
            "jev_status": "ok",
            "full_required": False,
            "snapshot_complete": True,
            "catalogue_digest": catalogue.digest,
            "base_sha": request.base_ref,
            "head_sha": git(Path(request.workspace), "rev-parse", "HEAD").strip(),
        }

    monkeypatch.setattr(SelectionService, "select", broken_candidate)
    result = await replay_case(
        GitManager(), str(history[0]), case(history), service=selector(), workdir=tmp_path / "runs"
    )
    assert result.critical_miss
    assert result.missing["mandatory_plus_jev"] == ("tests/test_a.py",)
    assert score([result], split_at=100).critical_misses == (result.commit,)


async def test_recorded_base_must_match_selection_merge_base(history, tmp_path):
    result = await replay_case(
        GitManager(),
        str(history[0]),
        case(history, base=history[2][-1]),
        service=selector(),
        workdir=tmp_path / "runs",
    )
    assert result.evidence_missing and not result.usable


async def test_offline_cancellation_finishes_read_before_cleanup(history, tmp_path, monkeypatch):
    import asyncio
    import threading

    entered, release = threading.Event(), threading.Event()
    paths = []

    def reading(workspace, **kwargs):
        paths.append(Path(workspace))
        entered.set()
        assert release.wait(timeout=10)
        assert paths[0].exists()
        return {}

    monkeypatch.setattr("src.test_selection.replay.select_offline", reading)
    task = asyncio.create_task(
        replay_case(
            GitManager(),
            str(history[0]),
            case(history),
            service=None,
            workdir=tmp_path / "runs",
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        task.cancel()
        await asyncio.sleep(0)
        assert paths[0].exists()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list((tmp_path / "runs").iterdir()) == []


@pytest.mark.parametrize(
    "ids", [frozenset({"other.py::test_alpha"}), frozenset({"tests/test_a.py"})]
)
async def test_inconsistent_failure_labels_are_not_usable(history, tmp_path, ids):
    result = await replay_case(
        GitManager(),
        str(history[0]),
        case(history, failing_node_ids=ids),
        service=selector(),
        workdir=tmp_path / "runs",
    )
    assert result.evidence_missing and not result.usable


async def test_failure_module_absent_from_historical_catalogue_is_not_usable(history, tmp_path):
    result = await replay_case(
        GitManager(),
        str(history[0]),
        case(
            history,
            failing_modules=frozenset({"tests/test_expired.py"}),
            failing_node_ids=frozenset({"tests/test_expired.py::test_old"}),
        ),
        service=selector(),
        workdir=tmp_path / "runs",
    )
    assert result.evidence_missing and not result.usable


def outcome(index=0, **changes):
    selectors = {"fallback": frozenset({"a", "b"}), "mandatory_plus_jev": frozenset({"a", "b"})}
    return replace(
        ReplayOutcome(
            str(index),
            selectors,
            {s: True for s in selectors},
            {s: () for s in selectors},
            False,
            False,
            False,
            False,
            "ok",
            observed_at=float(index),
            failing_modules=frozenset({"a", "b"}),
        ),
        **changes,
    )


def test_wilson_bounds_and_validation():
    assert wilson(30, 30) == pytest.approx((0.88648, 1.0), abs=0.00001)
    assert wilson(0, 0) == (0.0, 1.0)
    assert wilson(0, 30)[0] == 0
    for args in ((-1, 30), (31, 30), (0, -1), (1, 3, float("nan"))):
        with pytest.raises(ValueError):
            wilson(*args)


def test_chronological_split_and_promotion_gate():
    outcomes = [outcome(i, full_required=(i % 2 == 0)) for i in range(40)]
    report = score(outcomes, split_at=10)
    assert (report.usable, report.tune, report.held_out) == (40, 10, 30)
    assert report.promotion_bar_met
    assert report.recall["mandatory_plus_jev"]["hub"]["usable"] == 15
    assert report.recall["mandatory_plus_jev"]["non_hub"]["usable"] == 15
    assert report.recall["mandatory_plus_jev"]["wilson95"] == pytest.approx(wilson(60, 60))
    markdown = report.render_markdown()
    assert "30" in markdown and "Wilson" in markdown and "Runtime savings: not measured" in markdown
    assert not score(outcomes, split_at=11).promotion_bar_met
    outcomes[0] = replace(outcomes[0], critical_miss=True)
    report = score(outcomes, split_at=10)
    assert report.critical_misses == ("0",) and not report.promotion_bar_met


def test_module_recall_is_distinct_from_whole_commit_coverage():
    outcomes = [outcome(i) for i in range(30)]
    outcomes[0] = replace(
        outcomes[0],
        selectors={"mandatory_plus_jev": frozenset({"a"})},
        covered={"mandatory_plus_jev": False},
        missing={"mandatory_plus_jev": ("b",)},
    )
    report = score(outcomes, split_at=0)
    metrics = report.recall["mandatory_plus_jev"]
    assert metrics["rate"] == pytest.approx(59 / 60)
    assert metrics["commit_rate"] == pytest.approx(29 / 30)
    assert report.promotion_bar_met  # module recall exceeds 98%, despite one non-critical miss


def test_unusable_cases_and_duplicate_commits_cannot_inflate_promotion():
    good = [outcome(i) for i in range(29)]
    report = score(
        [
            *good,
            outcome(30, anachronistic=True),
            outcome(31, evidence_missing=True),
            outcome(32, failing_modules=frozenset()),
        ],
        split_at=0,
    )
    assert (report.cases, report.usable, report.held_out) == (32, 29, 29)
    assert not report.promotion_bar_met
    with pytest.raises(ValueError, match="duplicate"):
        score([*good, good[0]], split_at=0)


def test_empty_offline_and_low_recall_samples_cannot_promote():
    assert not score([], split_at=0).promotion_bar_met
    assert not score(
        [outcome(i, jev_status="disabled") for i in range(30)], split_at=0
    ).promotion_bar_met
    missed = outcome(selectors={"mandatory_plus_jev": frozenset({"a"})})
    assert not score(
        [replace(missed, commit=str(i)) for i in range(30)], split_at=0
    ).promotion_bar_met


def test_fallback_and_unknown_abstention_rates_include_all_cases():
    report = score(
        [
            outcome(0, full_required=True, jev_status="disabled"),
            outcome(1, jev_status="unavailable"),
            outcome(2, area_decisions={"alpha": {"choice": "unknown"}}),
            outcome(3),
        ],
        split_at=0,
    )
    assert report.fallback_rate == 0.5
    assert report.abstention_rate == 0.75


def test_calibration_is_held_out_omitted_areas_only():
    omitted = {"p_unaffected": 0.99, "omitted": True, "choice": "unaffected"}
    prediction = outcome(
        area_decisions={"alpha": omitted, "beta": {**omitted, "omitted": False}},
        area_modules={"alpha": frozenset({"a"}), "beta": frozenset({"b"})},
    )
    report = score(
        [
            replace(prediction, commit="tune", observed_at=-1),
            prediction,
            replace(prediction, commit="missing", evidence_missing=True),
        ],
        split_at=0,
    )
    bucket = next(b for b in report.calibration if b["areas"])
    assert bucket["areas"] == bucket["failures"] == 1
    assert bucket["failure_rate"] == 1.0
    assert bucket["mean_p_unaffected"] == 0.99


def test_cli_offline_round_trip(history, tmp_path, monkeypatch):
    import runpy
    import sys

    namespace = runpy.run_path("scripts/replay-test-selection.py", run_name="replay_cli")
    cases = tmp_path / "cases.json"
    item = case(history)
    cases.write_text(
        json.dumps(
            [
                {
                    "commit": item.commit,
                    "base": item.base,
                    "observed_at": item.observed_at,
                    "source": item.source,
                    "failing_modules": list(item.failing_modules),
                    "failing_node_ids": list(item.failing_node_ids),
                }
            ]
        )
    )
    output = tmp_path / "report.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "replay-test-selection.py",
            "--repo",
            str(history[0]),
            "--cases",
            str(cases),
            "--split-at",
            "0",
            "--out",
            str(output),
        ],
    )
    assert namespace["main"]() == 0
    assert "Usable: 1" in output.read_text() and "promotion_bar_met: false" in output.read_text()


async def test_cli_live_jev_is_explicit_and_requires_environment_key(monkeypatch):
    import runpy
    from types import SimpleNamespace

    namespace = runpy.run_path("scripts/replay-test-selection.py", run_name="replay_cli")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(ValueError, match="--with-jev requires TYPESAFE_API_KEY"):
        await namespace["_replay"](SimpleNamespace(with_jev=True), [])
