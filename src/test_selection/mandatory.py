"""The mandatory set ``M``: what a change must run whatever any model says.

Spec §4.2.  Every path the snapshot touched, new and old sides alike, is
mapped by the reviewed rules in a fixed order:

1. a global invalidator makes ``M`` the whole universe;
2. a catalogued test module runs itself, and names its areas; a deleted or
   renamed-from one is not runnable, so its areas' surviving modules run
   in its place;
3. a scoped conftest runs every module under its subtree;
4. a non-behavioural path adds nothing more and forces no fallback;
5. an ownership rule runs every module of its areas;
6. a source-scanning trigger runs its scanning modules;
7. a ``src`` file runs every module that imports it directly.

A path none of these map is unmapped and makes ``M`` the universe, as does an
incomplete snapshot.  Any behavioural match adds the critical ratchets.  A
module the snapshot deleted never enters ``M`` through a rule: it is in no
argument array.

Each module carries the reason codes (:mod:`src.test_selection.reasons`)
that put it there, in the order the paths were mapped.  The mapping order
and answers are mirrored by ``catalogue._is_mapped``, which the rules
coverage check uses to name paths that would force a full run.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from src.test_selection import reasons as r
from src.test_selection.catalogue import Catalogue, Rules, dotted_name, match_glob
from src.test_selection.snapshot import ChangeSnapshot


@dataclass(frozen=True)
class MandatoryResult:
    modules: frozenset[str]  # M; == catalogue.universe when full_required
    full_required: bool
    reasons: dict[str, tuple[str, ...]]  # module -> reason codes, one entry per module of M
    global_reasons: tuple[str, ...]  # why M = U, e.g. ("global_invalidator:pyproject.toml",)
    affected_areas: frozenset[str]  # areas an ownership rule or a changed test pointed at
    unmapped_paths: tuple[str, ...]


class _Collector:
    """Module -> ordered, de-duplicated reasons; never admits a removed module."""

    def __init__(self, removed: frozenset[str]) -> None:
        self._removed = removed
        self.reasons: dict[str, list[str]] = {}

    def add(self, modules: Iterable[str], reason: str) -> None:
        for module in sorted(modules):
            if module in self._removed:
                continue
            found = self.reasons.setdefault(module, [])
            if reason not in found:
                found.append(reason)


def _any_match(patterns: Iterable[str], path: str) -> bool:
    return any(match_glob(pattern, path) for pattern in patterns)


def _importers(catalogue: Catalogue) -> dict[str, list[str]]:
    """Dotted ``src`` name -> the catalogued modules importing it directly."""
    index: dict[str, list[str]] = {}
    for module, info in catalogue.modules.items():
        for name in info.imports:
            index.setdefault(name, []).append(module)
    return index


def _full(
    catalogue: Catalogue,
    specific: dict[str, list[str]],
    global_reasons: tuple[str, ...],
    affected_areas: Iterable[str],
    unmapped: tuple[str, ...],
) -> MandatoryResult:
    """``M = U``: every module keeps its own reasons and gains the global codes."""
    codes: list[str] = []
    for reason in global_reasons:
        if r.code_of(reason) not in codes:
            codes.append(r.code_of(reason))
    reasons = {}
    for module in sorted(catalogue.universe):
        own = specific.get(module, [])
        reasons[module] = (*own, *(c for c in codes if c not in own))
    return MandatoryResult(
        modules=catalogue.universe,
        full_required=True,
        reasons=reasons,
        global_reasons=global_reasons,
        affected_areas=frozenset(affected_areas),
        unmapped_paths=unmapped,
    )


def mandatory_set(
    snapshot: ChangeSnapshot,
    catalogue: Catalogue,
    rules: Rules,
    *,
    base_catalogue: Catalogue | None = None,
) -> MandatoryResult:
    """Map changed paths; use the base catalogue to find removed tests' old areas."""
    if not snapshot.complete:
        why = snapshot.incomplete_reason
        reason = r.with_detail(r.SNAPSHOT_INCOMPLETE, why) if why else r.SNAPSHOT_INCOMPLETE
        return _full(catalogue, {}, (reason,), (), ())

    live = frozenset(c.path for c in snapshot.changes if c.status != "deleted")
    removed = snapshot.paths - live
    importers = _importers(catalogue)
    collected = _Collector(removed)
    areas: set[str] = set()
    invalidated: list[str] = []
    unmapped: list[str] = []
    behavioural = False

    for path in sorted(snapshot.paths):
        if _any_match(rules.global_invalidators, path):
            invalidated.append(r.with_detail(r.GLOBAL_INVALIDATOR, path))
            continue
        matched = False
        test_catalogue = catalogue if path in live else base_catalogue or catalogue
        if path in test_catalogue.modules:
            matched = True
            owners = test_catalogue.areas_for_module(path)
            areas.update(owners)
            if path in live:
                collected.add([path], r.MANDATORY_CHANGED_TEST)
            else:
                # Not runnable any more; what it checked still has to run.
                collected.add(
                    catalogue.modules_for_areas(owners),
                    r.with_detail(r.MANDATORY_CHANGED_TEST, path),
                )
        for scoped in rules.scoped_conftests:
            if scoped.path == path:
                matched = True
                prefix = scoped.subtree.rstrip("/") + "/"
                collected.add(
                    (m for m in catalogue.modules if m.startswith(prefix)),
                    r.with_detail(r.MANDATORY_SCOPED_CONFTEST, path),
                )
        if _any_match(rules.non_behavioral, path):
            behavioural = behavioural or matched
            continue
        for rule in rules.ownership:
            if _any_match(rule.paths, path):
                matched = True
                for area_id in rule.areas:
                    areas.add(area_id)
                    collected.add(
                        catalogue.modules_for_areas([area_id]),
                        r.with_detail(r.MANDATORY_RULE, area_id),
                    )
        for rule in rules.source_scanning:
            if _any_match(rule.triggers, path):
                matched = True
                collected.add(rule.modules, r.MANDATORY_SOURCE_SCAN)
        dotted = dotted_name(path) if path.startswith("src/") else None
        if dotted is not None and dotted in importers:
            matched = True
            collected.add(importers[dotted], r.with_detail(r.MANDATORY_DIRECT_IMPORT, path))
        if matched:
            behavioural = True
        else:
            unmapped.append(path)

    if behavioural:
        collected.add(rules.critical, r.MANDATORY_CRITICAL)

    global_reasons = tuple(invalidated) + tuple(
        r.with_detail(r.UNMAPPED_PATH, path) for path in unmapped
    )
    if global_reasons:
        return _full(catalogue, collected.reasons, global_reasons, areas, tuple(unmapped))
    return MandatoryResult(
        modules=frozenset(collected.reasons),
        full_required=False,
        reasons={m: tuple(collected.reasons[m]) for m in sorted(collected.reasons)},
        global_reasons=(),
        affected_areas=frozenset(areas),
        unmapped_paths=(),
    )
