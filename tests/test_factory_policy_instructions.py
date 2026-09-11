"""Shipped instructions stay in line with the software-factory policy.

``docs/concepts/factory-policy.md`` is the one normative statement of how AQ
admits, delivers and recovers work (vault spec
``projects/agent-queue/specs/software-factory-policy-simplification-2026-09-11.md``,
§ Instructions). Shipped profiles and skills are seeded write-if-absent, so a
stale default is re-seeded onto every fresh install and every operator
``profile-reseed``. These checks fail as soon as a shipped instruction revives
a retired stage, the retired sibling-by-default placement or a refusal bypass,
or a role stops pointing at the policy — and they pin the shipped routes so an
instruction cleanup cannot quietly change a provider or class.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.profiles.parser import parse_profile

REPO = Path(__file__).resolve().parent.parent
POLICY = REPO / "docs" / "concepts" / "factory-policy.md"
POLICY_REF = "docs/concepts/factory-policy.md"
DEFAULTS = REPO / "src" / "profiles" / "defaults"


def _instruction_sources() -> list[Path]:
    """Every shipped file an agent reads as instructions."""
    paths = {
        *DEFAULTS.glob("*/profile.md"),
        *(REPO / "src" / "skills").glob("*/SKILL.md"),
        *(REPO / "src" / "prime" / "templates").glob("*.md"),
        *(REPO / "src" / "prompts").glob("*.md"),
        *(REPO / "src" / "prompts" / "default_playbooks").glob("*.md"),
        *(REPO / "src" / "prompts" / "project_playbooks").rglob("*.md"),
        REPO / "AGENTS.md",
    }
    return sorted(paths)


def _flat(path: Path) -> str:
    """File text with whitespace collapsed, so a phrase split across lines matches."""
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8"))


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


#: Phrases that restate retired policy.  Each key names what the phrase
#: promised; the pattern matches the directive, not a sentence denying it.
RETIRED = {
    "automatic reviewer stage": r"reviewer stage (runs|reads|enforces)",
    "final-reviewer stage": r"final-reviewer (stage|merges)",
    "per-task review chain": r"after all per-task reviewers? approve",
    "exclusive merge authority": r"only profile (with|carrying) `?pr_merge",
    "obsolete close outcome": r"outcome=(success|failure|needs_context)",
    "gh merge outside AQ": r"gh pr merge (<|--|\d)|merge it immediately",
    "merge without validation": r"without running tests|do not run tests for clean merges",
    "daemon refusal bypass": r"scope check rejects it",
    "sibling-by-default placement": (
        r"placed as your sibling|sibling under that same parent|siblings? of the gate"
    ),
    "worker-owned exit PR": r"owns opening the",
}

#: Roles whose instructions must apply the policy rather than restate workflow.
POLICY_ROLES = (
    "worker-standard-medium-claude",
    "worker-deep-high-claude",
    "worker-fast-medium-claude",
    "reviewer",
    "final-reviewer",
    "pr-merger",
    "supervisor",
)

#: The shipped provider/class of every default profile.  Instruction edits must
#: not change these; a deliberate route change updates this table with it.
SHIPPED_ROUTES = {
    "final-reviewer": ("claude", "standard-medium"),
    "planner": ("claude", None),
    "playbook-compiler": ("claude", "fast-low"),
    "pr-merger": ("codex", "deep-medium"),
    "reviewer": ("claude", "standard-low"),
    "spec-ingest": ("claude", "deep-high"),
    "supervisor": ("claude", "deep-high"),
    "triage": ("claude", "fast-low"),
    "worker-deep-high-claude": ("claude", "deep-high"),
    "worker-fast-medium-claude": ("claude", "fast-medium"),
    "worker-standard-medium-claude": ("claude", "standard-medium"),
}


def test_policy_is_one_short_normative_page():
    text = POLICY.read_text(encoding="utf-8")
    assert "**Status: normative.**" in text
    for heading in ("## A. ", "## B. ", "## C. "):
        assert heading in text
    flat = _flat(POLICY).lower()
    assert "child of the task that exposed it" in flat
    assert "no automatic" in flat
    assert "one configured publisher" in flat
    assert "never a pass" in flat
    # Short on purpose: role instructions reference it instead of copying it.
    assert len(text.splitlines()) <= 90


@pytest.mark.parametrize("source", _instruction_sources(), ids=_rel)
def test_shipped_instruction_restates_no_retired_policy(source: Path):
    text = _flat(source)
    hits = {
        name: match.group(0)
        for name, pattern in RETIRED.items()
        if (match := re.search(pattern, text, flags=re.IGNORECASE))
    }
    assert not hits, f"{_rel(source)} restates retired policy: {hits}"


@pytest.mark.parametrize("profile_id", POLICY_ROLES)
def test_role_instructions_reference_the_policy(profile_id: str):
    parsed = parse_profile((DEFAULTS / profile_id / "profile.md").read_text(encoding="utf-8"))
    assert parsed.is_valid, parsed.errors
    assert POLICY_REF in f"{parsed.role}\n{parsed.rules}", profile_id


def test_shipped_routes_are_unchanged():
    routes = {}
    for path in sorted(DEFAULTS.glob("*/profile.md")):
        parsed = parse_profile(path.read_text(encoding="utf-8"))
        routes[parsed.frontmatter.id] = (
            parsed.config.get("harness"),
            parsed.config.get("default_class"),
        )
    assert routes == SHIPPED_ROUTES


@pytest.mark.parametrize("profile_id", ("pr-merger", "final-reviewer"))
def test_merge_roles_require_validation_and_respect_refusals(profile_id: str):
    parsed = parse_profile((DEFAULTS / profile_id / "profile.md").read_text(encoding="utf-8"))
    prompt = re.sub(r"\s+", " ", f"{parsed.role}\n{parsed.rules}").lower()
    assert "required checks" in prompt
    assert "exact head" in prompt
    assert "never merge with `gh pr merge`" in prompt
    assert "delegates publication" in prompt


def test_no_shipped_playbook_schedules_a_merge_sweep():
    """The retired ``pr-merge-sweep`` recipe is not shipped or re-seedable."""
    prompts = REPO / "src" / "prompts"
    assert not (prompts / "project_playbooks" / "agent-queue" / "pr-merge-sweep.md").exists()
    assert not (REPO / "tests" / "fixtures" / "playbooks" / "v2" / "pr-merge-sweep").exists()
    for source in sorted(prompts.rglob("*.md")):
        assert "pr-merger" not in source.read_text(encoding="utf-8"), _rel(source)
