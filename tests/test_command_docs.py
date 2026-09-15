"""Drift guards for ``docs/reference/playbook-commands``.

The pages are half generated from the command registry and half hand-written.
These tests answer three questions: does every registered command have a page,
does every page still belong to a registered command, and is the generated
half of each page what the generator would write today.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from src.commands.contracts import CONTRACTS

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "gen-command-docs.py"
DOCS = REPO / "docs" / "reference" / "playbook-commands"


@pytest.fixture(scope="module")
def generator():
    spec = importlib.util.spec_from_file_location("gen_command_docs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def docs_copy(tmp_path: Path) -> Path:
    """A throwaway copy of the committed section, safe to mutate."""
    destination = tmp_path / "playbook-commands"
    shutil.copytree(DOCS, destination)
    return destination


def test_every_registered_command_has_a_page():
    missing = sorted(name for name in CONTRACTS.names() if not (DOCS / f"{name}.md").is_file())
    assert not missing, (
        "commands with no documentation page: "
        + ", ".join(missing)
        + " — run python scripts/gen-command-docs.py"
    )


def test_every_page_belongs_to_a_registered_command():
    pages = {path.stem for path in DOCS.glob("*.md")} - {"README"}
    orphans = sorted(pages - set(CONTRACTS.names()))
    assert not orphans, f"pages for commands that are no longer registered: {', '.join(orphans)}"


def test_the_committed_pages_match_a_fresh_generation(generator):
    assert generator.check(DOCS) == []


def test_the_index_lists_every_command_exactly_once(generator):
    index = (DOCS / "README.md").read_text(encoding="utf-8")
    for name in sorted(CONTRACTS.names()):
        assert index.count(f"[`{name}`]({name}.md)") == 1, f"{name} is not indexed exactly once"


def test_every_command_presentation_has_a_summary():
    empty = sorted(
        name
        for name in CONTRACTS.names()
        if not CONTRACTS.require(name).contract.presentation.summary.strip()
    )
    assert not empty, f"commands with an empty presentation summary: {', '.join(empty)}"


def test_a_stale_generated_block_is_reported(generator, docs_copy: Path):
    page = docs_copy / "gate_create.md"
    page.write_text(
        page.read_text(encoding="utf-8").replace("| Retry safe | no |", "| Retry safe | yes |"),
        encoding="utf-8",
    )

    problems = generator.check(docs_copy)

    assert [problem for problem in problems if problem.endswith("generated block is stale")]


def test_a_missing_page_and_an_unknown_page_are_reported(generator, docs_copy: Path):
    (docs_copy / "gate_resolve.md").unlink()
    (docs_copy / "retired_command.md").write_text("# retired\n", encoding="utf-8")

    problems = generator.check(docs_copy)

    assert any(problem.endswith("gate_resolve.md: missing") for problem in problems)
    assert any("retired_command.md: no command" in problem for problem in problems)


def test_regeneration_preserves_hand_written_sections(generator, docs_copy: Path):
    # Appending rather than rewriting a stub keeps this guard about the
    # generator's splice, not about whatever prose a page happens to carry.
    page = docs_copy / "gate_create.md"
    sentinel = "Hand-written prose a regeneration must not touch."
    page.write_text(
        page.read_text(encoding="utf-8") + f"\n## Sentinel\n\n{sentinel}\n", encoding="utf-8"
    )

    generator.write(docs_copy)

    assert sentinel in page.read_text(encoding="utf-8")
    assert generator.write(docs_copy) == []


def test_a_page_without_markers_fails_loudly_instead_of_being_overwritten(
    generator, docs_copy: Path
):
    page = docs_copy / "gate_create.md"
    page.write_text("# hand-written, no markers\n", encoding="utf-8")

    with pytest.raises(generator.MarkerError):
        generator.write(docs_copy)

    assert page.read_text(encoding="utf-8") == "# hand-written, no markers\n"
    assert generator.check(docs_copy) == [
        f"{page}: generated markers are missing or out of order"
    ]
