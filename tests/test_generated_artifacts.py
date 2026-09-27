"""Generated artifacts: one regeneration command, a drift check, merges that never conflict.

``scripts/regenerate-generated.sh`` rebuilds every committed generated artifact,
and ``.gitattributes`` marks the same set ``merge=aq-generated`` so that the
development publisher merges them without conflicting and rebuilds them after
the merge (``DevelopmentIntegration.merge_member``).  These tests keep the two
lists in step, show the attributes do what the publisher relies on for the real
paths, and run the drift check itself.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.integration.development import GENERATED_MERGE_CONFIG, GENERATED_MERGE_DRIVER

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "regenerate-generated.sh"
IDENTITY = ["-c", "user.name=Tester", "-c", "user.email=tester@example.test"]


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *IDENTITY, *args], capture_output=True, text=True, check=check
    )


def _listed() -> list[str]:
    return subprocess.run(
        [str(SCRIPT), "--list"], capture_output=True, text=True, check=True
    ).stdout.split()


def _marked() -> list[str]:
    """The patterns ``.gitattributes`` gives the generated-artifact merge driver."""
    marked = []
    for line in (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if fields and not fields[0].startswith("#") and (
            f"merge={GENERATED_MERGE_DRIVER}" in fields[1:]
        ):
            marked.append(fields[0])
    return marked


def test_gitattributes_marks_exactly_what_the_script_regenerates():
    """A generator added to one list and not the other is either never rebuilt
    at merge time or rebuilt without its conflicts ever being avoided."""
    assert sorted(p.removesuffix("/**") for p in _marked()) == sorted(_listed())


def test_every_regenerated_artifact_is_tracked_and_resolves_to_the_driver():
    for path in _listed():
        tracked = _git(ROOT, "ls-files", "--", path).stdout.split()
        assert tracked, f"{path} is listed as generated but nothing under it is tracked"
        attribute = _git(ROOT, "check-attr", "merge", "--", tracked[0]).stdout
        assert attribute.strip().endswith(f"merge: {GENERATED_MERGE_DRIVER}"), attribute


def test_the_script_is_committed_executable():
    """The publisher runs the policy's command directly, not through a shell."""
    mode = _git(ROOT, "ls-files", "-s", "--", "scripts/regenerate-generated.sh").stdout.split()[0]
    assert mode == "100755"


def test_an_unknown_argument_is_a_usage_error_not_a_regeneration():
    result = subprocess.run(
        [str(SCRIPT), "--bogus"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "unknown argument: --bogus" in result.stderr


def test_real_generated_paths_merge_without_conflicting(tmp_path):
    """Two branches each add a test module: the catalogue both regenerated and the
    area list both appended to.  Under the publisher's driver neither conflicts;
    without it only the union-merged area list merges on its own."""
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-q", "--initial-branch=main", str(repo))
    (repo / ".gitattributes").write_text(
        (ROOT / ".gitattributes").read_text(encoding="utf-8"), encoding="utf-8"
    )
    catalogue = repo / "tests" / "selection_catalogue.json"
    areas = repo / "tests" / "selection_areas.yaml"
    catalogue.parent.mkdir()

    def write(modules):
        entries = ",\n".join(f'    "{m}"' for m in modules)
        catalogue.write_text(
            f'{{\n  "count": {len(modules)},\n  "modules": [\n{entries}\n  ]\n}}\n',
            encoding="utf-8",
        )
        areas.write_text(
            "areas:\n  - id: area\n    match:\n"
            + "".join(f"      - {m}\n" for m in modules),
            encoding="utf-8",
        )

    write(["tests/test_a.py"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "one")
    write(["tests/test_a.py", "tests/test_b.py"])
    _git(repo, "commit", "-qam", "one")
    _git(repo, "checkout", "-q", "main")
    write(["tests/test_a.py", "tests/test_c.py"])
    _git(repo, "commit", "-qam", "main")

    plain = _git(repo, "merge", "--no-edit", "one", check=False)
    assert plain.returncode == 1
    unmerged = _git(repo, "diff", "--name-only", "--diff-filter=U").stdout.split()
    assert unmerged == ["tests/selection_catalogue.json"]
    _git(repo, "merge", "--abort")

    merged = _git(repo, *GENERATED_MERGE_CONFIG, "merge", "--no-edit", "one", check=False)
    assert merged.returncode == 0, merged.stdout + merged.stderr
    # Our side of every overlapping hunk: stale until the publisher regenerates it.
    assert '"tests/test_c.py"' in catalogue.read_text(encoding="utf-8")
    assert "<<<<<<<" not in catalogue.read_text(encoding="utf-8")
    listed = areas.read_text(encoding="utf-8")
    assert "tests/test_b.py" in listed and "tests/test_c.py" in listed


@pytest.mark.slow
def test_committed_generated_files_match_regeneration():
    """The one-command drift check: every generator, run in a scratch copy."""
    result = subprocess.run(
        [str(SCRIPT), "--check"], capture_output=True, text=True, timeout=900, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
