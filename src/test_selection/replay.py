"""Blind historical selection and chronological scoring, never policy promotion.

Failure labels remain outside selector inputs. Exact baseline node IDs are
excluded only during scoring; no new failures is not a usable red commit.
The recall gate here does not measure the separate runtime-savings release bar.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from src.git.manager import GitError, GitManager
from src.test_selection.catalogue import (
    CATALOGUE_PATH,
    RULES_PATH,
    CatalogueError,
    load_catalogue_text,
    load_rules,
)
from src.test_selection.service import SelectionRequest, SelectionService, select_offline


@dataclass(frozen=True)
class ReplayCase:
    commit: str
    base: str
    failing_modules: frozenset[str]
    failing_node_ids: frozenset[str]
    observed_at: float
    source: str
    known_failures: frozenset[str] = frozenset()
    artifact_expired: bool = False
    ambiguous_cause: bool = False


@dataclass(frozen=True)
class ReplayOutcome:
    commit: str
    selectors: dict[str, frozenset[str]]
    covered: dict[str, bool]
    missing: dict[str, tuple[str, ...]]
    full_required: bool
    evidence_missing: bool
    anachronistic: bool
    critical_miss: bool
    jev_status: str
    observed_at: float = 0.0
    failing_modules: frozenset[str] = frozenset()
    area_decisions: dict[str, dict] = field(default_factory=dict)
    area_modules: dict[str, frozenset[str]] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return bool(self.failing_modules) and not (self.evidence_missing or self.anachronistic)


async def _commit(git: GitManager, repo: str, ref: str) -> str:
    # Resolve once to a commit, without fetching or accepting option injection.
    result = await git.arun_git_result(
        ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"], cwd=repo
    )
    if result.returncode:
        raise GitError(f"replay commit unavailable: {ref}")
    return result.stdout.strip()


async def replay_case(
    git: GitManager,
    repo: str,
    case: ReplayCase,
    *,
    service: SelectionService | None,
    workdir: Path,
    existing_worker_selection: frozenset[str] | None = None,
) -> ReplayOutcome:
    """Select a detached historical tree before opening any outcome labels.

    Only commit/base go into the request. Existing worker choices are comparison
    labels, never explicit targets that could influence mandatory membership.
    Offline's synchronous asyncio entry point runs in a separate thread.
    """
    repo = str(Path(repo).resolve())
    commit = await _commit(git, repo, case.commit)
    base = await _commit(git, repo, case.base)
    workspace = workdir.resolve() / f"{commit}-{uuid.uuid4().hex}"
    await git.aworktree_add(repo, str(workspace), ref=commit, detach=True)
    try:
        if service is None:
            operation = asyncio.create_task(
                asyncio.to_thread(select_offline, str(workspace), base_ref=base, targets=())
            )
            try:
                result = await asyncio.shield(operation)
            except asyncio.CancelledError:
                # A thread cannot be cancelled. Let its read finish before
                # removing the historical tree it is still inspecting.
                await asyncio.gather(operation, return_exceptions=True)
                raise
        else:
            request = SelectionRequest(
                "replay", None, None, None, str(workspace), "shadow", base, (), True
            )
            result = await service.select(request, persist=False)

        # The selector has finished. Only now read labels and scoring metadata.
        labels = case.failing_node_ids
        known = case.known_failures
        modules = frozenset(node.partition("::")[0] for node in labels)
        evidence_missing = (
            not labels
            or any("::" not in node or not node.partition("::")[2] for node in labels | known)
            or modules != case.failing_modules
            or case.artifact_expired
            or case.ambiguous_cause
            or not result.get("snapshot_complete", False)
            or result.get("base_sha") != base
            or result.get("head_sha") != commit
        )
        failures = frozenset(node.partition("::")[0] for node in labels - known)
        full = bool(result["full_required"])
        fallback = frozenset(result["fallback_modules"])
        mandatory = frozenset(result["mandatory_modules"])
        jev = result.get("jev_modules")
        selectors = {
            "fallback": fallback,
            # Failed/disabled Jev has operational fallback semantics. Do not
            # invent an analytical Jev-only prediction for an absent answer.
            "mandatory_plus_jev": mandatory | frozenset(jev) if jev is not None else fallback,
        }
        if jev is not None:
            selectors["jev_alone"] = frozenset(jev)
        if existing_worker_selection is not None:
            selectors["existing_worker"] = frozenset(existing_worker_selection)
        missing = {s: tuple(sorted(failures - selected)) for s, selected in selectors.items()}
        covered = {s: bool(failures) and not omitted for s, omitted in missing.items()}

        historical = await git.arun_git_result(["show", f"{commit}:{CATALOGUE_PATH}"], cwd=repo)
        anachronistic = True
        critical = frozenset()
        area_modules = {}
        try:
            if historical.returncode:
                raise CatalogueError(["no contemporaneous catalogue"])
            catalogue = load_catalogue_text(historical.stdout, source=f"{commit}:{CATALOGUE_PATH}")
            anachronistic = result.get("catalogue_digest") != catalogue.digest
            evidence_missing = evidence_missing or not modules <= catalogue.modules.keys()
            rules = await asyncio.to_thread(load_rules, workspace / RULES_PATH, catalogue)
            critical = frozenset(rules.critical)
            area_modules = {a: frozenset(info.modules) for a, info in catalogue.areas.items()}
        except CatalogueError:
            evidence_missing = True

        return ReplayOutcome(
            commit=commit,
            selectors=selectors,
            covered=covered,
            missing=missing,
            full_required=full,
            evidence_missing=evidence_missing,
            anachronistic=anachronistic,
            # Only a miss in the operational omission candidate triggers the
            # critical guard. J alone intentionally lacks mandatory protection.
            critical_miss=bool(critical.intersection(missing["mandatory_plus_jev"])),
            jev_status=result["jev_status"],
            observed_at=case.observed_at,
            failing_modules=failures,
            area_decisions=result.get("area_decisions", {}),
            area_modules=area_modules,
        )
    finally:
        # Cancellation must not strand the worktree registration. Shield the
        # cleanup and finish it before propagating cancellation to the caller.
        cleanup = asyncio.create_task(git.aremove_worktree(repo, str(workspace)))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise


def wilson(covered: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval; an empty sample communicates complete uncertainty."""
    if (
        not isinstance(covered, int)
        or not isinstance(n, int)
        or n < 0
        or not 0 <= covered <= n
        or not math.isfinite(z)
        or z <= 0
    ):
        raise ValueError("invalid Wilson counts or z")
    if n == 0:
        return (0.0, 1.0)
    p = covered / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return (
        0.0 if covered == 0 else max(0.0, centre - half),
        1.0 if covered == n else min(1.0, centre + half),
    )


def _metrics(outcomes: Sequence[ReplayOutcome], selector: str) -> dict:
    applicable = [o for o in outcomes if selector in o.selectors]
    total = sum(len(o.failing_modules) for o in applicable)
    selected = sum(len(o.failing_modules & o.selectors[selector]) for o in applicable)
    commits = sum(o.failing_modules <= o.selectors[selector] for o in applicable)
    n = len(applicable)
    return {
        "covered": commits,
        "usable": n,
        "selected_failures": selected,
        "failing_modules": total,
        "rate": selected / total if total else 0.0,
        "wilson95": wilson(selected, total),
        "commit_rate": commits / n if n else 0.0,
        "commit_wilson95": wilson(commits, n),
    }


def _calibration(outcomes: Sequence[ReplayOutcome]) -> list[dict]:
    buckets = []
    for lo, hi in ((0.0, 0.5), (0.5, 0.9), (0.9, 0.98), (0.98, 1.0)):
        probabilities, failures = [], 0
        for outcome in outcomes:
            for area, decision in outcome.area_decisions.items():
                p = decision.get("p_unaffected")
                if not decision.get("omitted") or p is None:
                    continue
                if lo <= p < hi or hi == 1.0 and lo <= p <= hi:
                    if area not in outcome.area_modules:
                        continue
                    probabilities.append(p)
                    failures += bool(outcome.failing_modules & outcome.area_modules[area])
        n = len(probabilities)
        buckets.append(
            {
                "lo": lo,
                "hi": hi,
                "areas": n,
                "failures": failures,
                "mean_p_unaffected": sum(probabilities) / n if n else None,
                "failure_rate": failures / n if n else None,
                "wilson95": wilson(failures, n),
            }
        )
    return buckets


@dataclass(frozen=True)
class ReplayReport:
    cases: int
    usable: int
    tune: int
    held_out: int
    recall: dict[str, dict]
    critical_misses: tuple[str, ...]
    calibration: list[dict]
    fallback_rate: float
    abstention_rate: float
    promotion_bar_met: bool
    anachronistic: int = 0
    evidence_missing: int = 0
    baseline_only: int = 0

    def render_markdown(self) -> str:
        lines = [
            "# Test selection replay evaluation",
            "",
            f"Cases: {self.cases}; Usable: {self.usable}; "
            f"Tune: {self.tune}; Held-out: {self.held_out}.",
            f"Excluded flags: anachronistic={self.anachronistic}, "
            f"evidence_missing={self.evidence_missing}, baseline_only={self.baseline_only}.",
            "",
            "Held-out failing-module recall (Wilson 95%); whole-commit coverage separately.",
            "Jev-only is analytical; unavailable Jev uses fallback for mandatory_plus_jev.",
            "",
            "| Selector / stratum | Cases | Modules covered / failing | Recall | Wilson 95% "
            "| Commits covered | Commit Wilson 95% |",
            "| --- | ---: | ---: | ---: | --- | ---: | --- |",
        ]
        for selector, metrics in sorted(self.recall.items()):
            for stratum, item in (
                ("all", metrics),
                ("hub", metrics["hub"]),
                ("non_hub", metrics["non_hub"]),
            ):
                lo, hi = item["wilson95"]
                clo, chi = item["commit_wilson95"]
                lines.append(
                    f"| {selector} / {stratum} | {item['usable']} | "
                    f"{item['selected_failures']} / {item['failing_modules']} | "
                    f"{item['rate']:.3%} | [{lo:.4f}, {hi:.4f}] | "
                    f"{item['covered']} / {item['usable']} | [{clo:.4f}, {chi:.4f}] |"
                )
        lines.extend(
            [
                "",
                f"Fallback rate (all cases): {self.fallback_rate:.3%}; "
                f"abstention rate (all cases): {self.abstention_rate:.3%}.",
                "",
                "Held-out calibration: omitted areas only; P(unaffected) is an area "
                "judgment, not a calibrated failure probability.",
                "",
                "| P(unaffected) bucket | Areas | Failed areas | Mean P(unaffected) "
                "| Observed failure rate | Wilson 95% |",
                "| --- | ---: | ---: | ---: | ---: | --- |",
            ]
        )
        for bucket in self.calibration:
            mean = bucket["mean_p_unaffected"]
            rate = bucket["failure_rate"]
            lo, hi = bucket["wilson95"]
            mean_text = "n/a" if mean is None else f"{mean:.4f}"
            rate_text = "n/a" if rate is None else f"{rate:.3%}"
            boundary = "]" if bucket["hi"] == 1.0 else ")"
            lines.append(
                f"| [{bucket['lo']}, {bucket['hi']}{boundary} | {bucket['areas']} | "
                f"{bucket['failures']} | {mean_text} | {rate_text} | [{lo:.4f}, {hi:.4f}] |"
            )
        lines.extend(
            [
                "",
                "Critical misses in mandatory_plus_jev (any split): "
                + (", ".join(self.critical_misses) or "none"),
                f"promotion_bar_met: {str(self.promotion_bar_met).lower()} (recall gate only).",
                "Runtime savings: not measured. This report does not satisfy the complete "
                "release bar and never promotes or revokes a policy.",
                "Wilson intervals describe this sample; red commits are biased evidence, "
                "and module failures within a commit are not independent trials.",
            ]
        )
        return "\n".join(lines) + "\n"


def score(outcomes: Sequence[ReplayOutcome], *, split_at: float) -> ReplayReport:
    """Score usable held-out reds only; any critical miss defeats the recall gate."""
    if not math.isfinite(split_at) or any(not math.isfinite(o.observed_at) for o in outcomes):
        raise ValueError("chronological split requires finite timestamps")
    if len({o.commit for o in outcomes}) != len(outcomes):
        raise ValueError("duplicate commit in replay sample")
    usable = [o for o in outcomes if o.usable]
    held_out = [o for o in usable if o.observed_at >= split_at]
    selectors = sorted({s for o in outcomes for s in o.selectors})
    recall = {}
    for selector in selectors:
        recall[selector] = {
            **_metrics(held_out, selector),
            "hub": _metrics([o for o in held_out if o.full_required], selector),
            "non_hub": _metrics([o for o in held_out if not o.full_required], selector),
        }
    critical = tuple(sorted(o.commit for o in outcomes if o.critical_miss))
    candidate = recall.get("mandatory_plus_jev", {})
    n = len(outcomes)
    return ReplayReport(
        cases=n,
        usable=len(usable),
        tune=len(usable) - len(held_out),
        held_out=len(held_out),
        recall=recall,
        critical_misses=critical,
        calibration=_calibration(held_out),
        fallback_rate=sum(o.full_required or o.jev_status != "ok" for o in outcomes) / n
        if n
        else 0,
        abstention_rate=sum(
            o.jev_status != "ok"
            or any(d.get("choice") == "unknown" for d in o.area_decisions.values())
            for o in outcomes
        )
        / n
        if n
        else 0,
        promotion_bar_met=(
            len(held_out) >= 30
            and candidate.get("usable") == len(held_out)
            and candidate.get("rate", 0) >= 0.98
            and not critical
            and any(o.jev_status == "ok" for o in held_out)
        ),
        anachronistic=sum(o.anachronistic for o in outcomes),
        evidence_missing=sum(o.evidence_missing for o in outcomes),
        baseline_only=sum(not o.failing_modules and not o.evidence_missing for o in outcomes),
    )
