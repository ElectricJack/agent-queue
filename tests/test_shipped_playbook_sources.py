"""Shipped playbook Markdown is a prose authoring source, never a machine graph.

Package 6 §5.2 (T-4).  V2 compiles prose; an embedded ```json action graph in a
shipped source is a second, unreviewed authority for what the fleet executes.
These assertions are the ones that keep the prose rewrite honest:

* no *installed* source carries an action block (assertion 1);
* the classifier that decides "action block" is itself pinned, so rule 1 cannot
  be satisfied later by quietly loosening it (assertion 2);
* every Markdown file under ``src/prompts/`` is classified by exactly one
  declared root, so a new prompt directory fails this suite until a human says
  what it is (assertion 3);
* every identifier the reviewed artifact emits is granted by the prose
  (assertion 4) — prose that drops ``gate_create`` may not compile to a graph
  that calls it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Roots whose Markdown ``src/vault.py`` copies into a live vault.
INSTALLED_SOURCE_ROOTS = (
    "src/prompts/default_playbooks",
    "src/prompts/default_agent_type_playbooks",
)

#: Playbook-shaped Markdown that no code path installs.  The child plan drafted
#: this against ``src/prompts/example_playbooks/`` and ``src/prompts/default_rules/``;
#: neither directory exists on the live tree any more (reconciliation, §2).  The
#: tuple stays so a reintroduced sample root is classified rather than silently
#: swept into the installed corpus by assertion 3.
EXCLUDED_SAMPLE_ROOTS: tuple[str, ...] = ()

#: Prompt Markdown that is not a playbook at all.
NON_PLAYBOOK_PROMPTS = (
    "src/prompts/consolidation_task.md",
    "src/prompts/default_intelligence_classes",
    "src/prompts/execution_focus.md",
    "src/prompts/plan_structure_guide.md",
    "src/prompts/supervisor_system.md",
)

_FENCE = re.compile(r"^(?P<indent> *)```(?P<info>[A-Za-z0-9_+-]*) *$")


def is_action_block(fence_body: str) -> bool:
    """True when a ```json fence is an executable graph rather than an example.

    An action block is a JSON *object* carrying ``rules`` or ``nodes`` at the
    top level — the two keys the V1 pipeline and assignment compilers read.  A
    fence that merely illustrates an output shape (``{"targets": [...]}``) is
    documentation and stays.
    """
    try:
        parsed = json.loads(fence_body)
    except (ValueError, RecursionError):
        return False
    if not isinstance(parsed, dict):
        return False
    return "rules" in parsed or "nodes" in parsed


def iter_json_fences(text: str):
    """Yield ``(start_line, body)`` for every ```json fence, 1-based."""
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        opening = _FENCE.match(lines[index])
        if opening is None or opening.group("info").lower() != "json":
            index += 1
            continue
        closing = f"{opening.group('indent')}```"
        end = index + 1
        while end < len(lines) and lines[end].rstrip() != closing:
            end += 1
        yield index + 1, "\n".join(lines[index + 1 : end])
        index = end + 1


def _markdown_under(rel_root: str) -> list[Path]:
    root = REPO_ROOT / rel_root
    if root.is_file():
        return [root] if root.suffix == ".md" else []
    return sorted(root.rglob("*.md"))


def installed_sources() -> list[Path]:
    return [path for root in INSTALLED_SOURCE_ROOTS for path in _markdown_under(root)]


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


@pytest.mark.parametrize(
    "source", installed_sources(), ids=lambda p: _rel(p)
)
def test_no_installed_source_has_an_action_block(source: Path) -> None:
    offenders = [
        f"{_rel(source)}:{line}"
        for line, body in iter_json_fences(source.read_text(encoding="utf-8"))
        if is_action_block(body)
    ]
    assert not offenders, (
        "shipped playbook Markdown must be prose only; embedded action graph at "
        + ", ".join(offenders)
    )


def test_classifier_distinguishes_examples() -> None:
    """The classifier is pinned so assertion 1 cannot be loosened unnoticed."""
    assert is_action_block('{"rules": [{"id": "r", "on": "task.completed"}]}')
    assert is_action_block('{"nodes": {"done": {"terminal": true}}}')
    # The two output-shape examples in memory-consolidation.md.
    assert not is_action_block('{"targets": [{"scope": "project", "id": "p1"}]}')
    assert not is_action_block('{"tasks_created": ["t1", "t2"]}')
    # Not JSON, and JSON that is not an object.
    assert not is_action_block("not json at all")
    assert not is_action_block('["rules"]')


def test_excluded_roots_are_declared_not_forgotten() -> None:
    declared = INSTALLED_SOURCE_ROOTS + EXCLUDED_SAMPLE_ROOTS + NON_PLAYBOOK_PROMPTS
    unclassified: list[str] = []
    for path in sorted((REPO_ROOT / "src" / "prompts").rglob("*.md")):
        rel = _rel(path)
        matches = [root for root in declared if rel == root or rel.startswith(root + "/")]
        assert len(matches) <= 1, f"{rel} is claimed by more than one root: {matches}"
        if not matches:
            unclassified.append(rel)
    assert not unclassified, (
        "every Markdown file under src/prompts/ must be claimed by exactly one of "
        "INSTALLED_SOURCE_ROOTS, EXCLUDED_SAMPLE_ROOTS or NON_PLAYBOOK_PROMPTS; "
        f"unclassified: {unclassified}"
    )


def test_excluded_sample_roots_do_not_exist_or_are_real() -> None:
    """A declared sample root that has been deleted must be removed from the tuple."""
    missing = [root for root in EXCLUDED_SAMPLE_ROOTS if not (REPO_ROOT / root).exists()]
    assert not missing, f"EXCLUDED_SAMPLE_ROOTS names paths that no longer exist: {missing}"


@pytest.mark.parametrize("source", installed_sources(), ids=lambda p: _rel(p))
def test_shipped_sources_declare_every_identifier(source: Path) -> None:
    """Every external identifier the reviewed artifact emits is granted by prose.

    Delegates to the real compiler-side check (``validate_definition`` with the
    source's :class:`IdentifierInventory`) rather than reimplementing it, so the
    fixture and the compiler can never disagree about what "declared" means.

    A source with no *approved* artifact has nothing to check here; that it has
    none is asserted by :func:`test_every_source_without_an_artifact_is_blocked`
    and by ``tests/test_default_playbook_v2_artifacts.py``, so skipping is a
    narrow gap rather than a silent one.
    """
    from src.playbooks.authoring import PlaybookSource, SourceError
    from src.playbooks.definition import load_definition_json
    from src.playbooks.validation import (
        NullProfileLookup,
        RegisteredEventLookup,
        RegistryContractLookup,
        validate_definition,
    )
    from tests.test_default_playbook_v2_artifacts import FIXTURE_ROOT, playbook_id_for

    artifact_path = FIXTURE_ROOT / playbook_id_for(source) / "artifact.json"
    if not artifact_path.exists():
        pytest.skip(f"{_rel(source)} has no approved V2 artifact yet")

    loaded = PlaybookSource.load(source, vault_root=source.parent)
    assert not isinstance(loaded, SourceError), getattr(loaded, "errors", ())

    definition = load_definition_json(artifact_path.read_text(encoding="utf-8"))
    diagnostics = validate_definition(
        definition,
        inventory=loaded.inventory,
        contracts=RegistryContractLookup(),
        profiles=NullProfileLookup(),
        events=RegisteredEventLookup(),
    )
    undeclared = [d for d in diagnostics if d.code == "unknown_identifier"]
    assert not undeclared, (
        f"{_rel(source)} does not grant every identifier its artifact emits: "
        + "; ".join(f"{d.rule_id or '-'}/{d.step_id or '-'}: {d.message}" for d in undeclared)
    )


@pytest.mark.parametrize("source", installed_sources(), ids=lambda p: _rel(p))
def test_every_source_without_an_artifact_is_blocked(source: Path) -> None:
    """No installed source may quietly lack a reviewed artifact.

    Either it has one, or its fixture says in writing what is blocking it — the
    roadmap's exit gate ("every non-ready playbook has a visible reason and
    operator decision") expressed as an assertion.
    """
    from tests.playbook_fixture_activation import review_frontmatter
    from tests.test_default_playbook_v2_artifacts import FIXTURE_ROOT, playbook_id_for

    directory = FIXTURE_ROOT / playbook_id_for(source)
    if (directory / "artifact.json").exists():
        return
    review = review_frontmatter(directory)
    assert review is not None, f"{_rel(source)} has neither an artifact nor a review record"
    assert review.get("blocked_on"), (
        f"{_rel(source)} has no reviewed artifact and no recorded blocker; "
        "a missing artifact must always be explained"
    )
