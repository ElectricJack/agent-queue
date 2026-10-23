"""Generated artifacts: one regeneration command, a drift check, merges that never conflict.

``scripts/regenerate-generated.sh`` rebuilds every committed generated artifact,
and ``.gitattributes`` marks the same set ``merge=aq-generated`` so that the
development publisher merges them without conflicting and rebuilds them after
the merge (``DevelopmentPrimitives.merge_member``).  These tests keep the two
lists in step, show the attributes do what the publisher relies on for the real
paths, and run the drift check itself.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from src.git.manager import GitError, GitManager
from src.integration.development import GENERATED_MERGE_CONFIG, GENERATED_MERGE_DRIVER
from src.integration.regeneration import GeneratedMergeConflict, merge_generated_tree
from src.test_selection import catalogue as cat

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "regenerate-generated.sh"
IDENTITY = ["-c", "user.name=Tester", "-c", "user.email=tester@example.test"]


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *IDENTITY, *args], capture_output=True, text=True, check=check,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
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
    def expand(patterns):
        return {
            path for pattern in patterns
            for path in _git(ROOT, "ls-files", "--", pattern.removesuffix("/**")).stdout.splitlines()
        }

    assert expand(_marked()) == expand(_listed())


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


def test_recorded_artifacts_preserve_approved_bytes_and_historical_policy(tmp_path):
    """Extracting source inputs must not reapprove policy or reset a baseline."""
    script = ROOT / "scripts/generate-recorded-artifacts.py"
    listed = subprocess.run(
        [sys.executable, str(script), "--list"], capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    expected = {name: (ROOT / name).read_bytes() for name in listed}
    shutil.copytree(ROOT / "tests/fixtures/generated_sources",
                    tmp_path / "tests/fixtures/generated_sources")
    shutil.copytree(ROOT / "tests/fixtures/contracts/command_audit_modules",
                    tmp_path / "tests/fixtures/contracts/command_audit_modules")
    for _ in range(2):
        subprocess.run([sys.executable, str(script), "--root", str(tmp_path)], check=True)
        assert {name: (tmp_path / name).read_bytes() for name in listed} == expected
    subprocess.run(
        [sys.executable, str(script), "--root", str(tmp_path), "--check"], check=True,
    )


@pytest.mark.parametrize("failure", ["missing_input", "unapproved_definition", "unapproved_source"])
def test_recorded_artifacts_refuse_missing_or_unapproved_inputs(tmp_path, failure):
    shutil.copytree(ROOT / "tests/fixtures/generated_sources",
                    tmp_path / "tests/fixtures/generated_sources")
    shutil.copytree(ROOT / "tests/fixtures/contracts/command_audit_modules",
                    tmp_path / "tests/fixtures/contracts/command_audit_modules")
    source = tmp_path / "tests/fixtures/generated_sources/playbooks/default-pipeline/definition.json"
    if failure == "missing_input":
        source.unlink()
    elif failure == "unapproved_definition":
        definition = json.loads(source.read_text())
        definition["compiled_at"] = "2000-01-01T00:00:00Z"
        source.write_text(json.dumps(definition))
    else:
        source.with_name("source.md").write_text("Unapproved replacement policy\n")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/generate-recorded-artifacts.py"),
         "--root", str(tmp_path)], capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "Recorded artifact input refused" in result.stderr
    assert not (tmp_path / "tests/integration_ownership.json").exists()


async def test_recorded_input_conflict_is_a_source_conflict(tmp_path):
    """Derived manifest conflicts never authorize choosing an approval input."""
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-q", "--initial-branch=main", str(repo))
    shutil.copyfile(ROOT / ".gitattributes", repo / ".gitattributes")
    source = "tests/fixtures/generated_sources/playbooks/default-pipeline/manifest.md"
    artifact = "src/prompts/reviewed_playbooks/default-pipeline/manifest.md"
    for name in (source, artifact):
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("approved provenance\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    heads = []
    for note in ("left", "right"):
        _git(repo, "checkout", "--detach", base)
        for name in (source, artifact):
            (repo / name).write_text(f"{note} provenance\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", note)
        heads.append(_git(repo, "rev-parse", "HEAD").stdout.strip())
    with pytest.raises(GeneratedMergeConflict) as raised:
        await merge_generated_tree(
            GitManager(), repo, ["merge-tree", "--write-tree", *heads],
        )
    assert source in raised.value.stdout
    assert not list(tmp_path.glob("aq-regen-*"))


@pytest.mark.parametrize("same_module", [False, True], ids=["independent", "same-evidence"])
@pytest.mark.parametrize("stale_outputs", [False, True], ids=["current", "stale-conflicts"])
async def test_hot_bookkeeping_merge_preserves_authored_evidence(
    tmp_path, same_module, stale_outputs,
):
    """Real branch merges rebuild aggregates, but never choose reviewed inputs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    shutil.copyfile(ROOT / ".gitattributes", repo / ".gitattributes")
    scripts = repo / "scripts"
    scripts.mkdir()
    shutil.copyfile(ROOT / "scripts/bookkeeping_sources.py", scripts / "bookkeeping_sources.py")
    generator = scripts / "regenerate-generated.sh"
    generator.write_text("#!/bin/sh\nexec python3 scripts/bookkeeping_sources.py\n")
    generator.chmod(0o755)

    def write_json(path, value):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, indent=2) + "\n")

    source = "tests/fixtures/generated_sources"
    for name in ("integration_ownership", "integration_ownership_report"):
        write_json(f"{source}/ownership/modules/{name}/policy/version.json", {"version": 1})
    descriptors = repo / source / "command_descriptors"
    descriptors.mkdir()
    for module in ("alpha", "beta"):
        for name in ("integration_ownership", "integration_ownership_report"):
            write_json(f"{source}/ownership/modules/{name}/modules/{module}.json",
                       {"shared": {f"src/{module}.py::owned": {"reason": "reviewed base"}}})
        write_json(f"tests/fixtures/contracts/command_audit_modules/src/{module}.py.json",
                   {module: {"producer": f"src/{module}.py:Handler._cmd_{module}",
                             "source_digest": "reviewed base", "review_notes": "keep approval"}})
        (descriptors / f"{module}.py.inc").write_text(f"def test_{module}():\n    assert 1\n\n")

    def regenerate():
        subprocess.run([str(generator)], cwd=repo, capture_output=True, text=True, check=True)

    regenerate()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    heads = []
    for side, module in (("left", "alpha"), ("right", "alpha" if same_module else "beta")):
        _git(repo, "checkout", "--detach", base)
        for name in ("integration_ownership", "integration_ownership_report"):
            write_json(f"{source}/ownership/modules/{name}/modules/{module}.json",
                       {"shared": {f"src/{module}.py::owned": {"reason": side}}})
        write_json(f"tests/fixtures/contracts/command_audit_modules/src/{module}.py.json",
                   {module: {"producer": f"src/{module}.py:Handler._cmd_{module}",
                             "source_digest": side, "review_notes": "keep approval"}})
        (descriptors / f"{module}.py.inc").write_text(f"def test_{module}():\n    assert {side!r}\n\n")
        regenerate()
        if stale_outputs:
            # A stale aggregate is disposable, including an overlapping change
            # whose old text cannot be parsed. Only the reviewed shards matter.
            (repo / "tests/test_command_descriptors.py").write_text(f"stale {side}\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", side)
        heads.append(_git(repo, "rev-parse", "HEAD").stdout.strip())
    args = ["merge-tree", "--write-tree", f"--merge-base={base}", *heads]
    if same_module or stale_outputs:
        assert _git(repo, *args, check=False).returncode == 1
    if same_module:
        with pytest.raises(GeneratedMergeConflict):
            await merge_generated_tree(GitManager(), repo, args)
        return
    tree = await merge_generated_tree(GitManager(), repo, args)
    for name in ("integration_ownership", "integration_ownership_report"):
        for path in (f"tests/{name}.json", f"{source}/ownership/{name}.json"):
            result = json.loads(_git(repo, "show", f"{tree}:{path}").stdout)
            assert result["shared"] == {
                "src/alpha.py::owned": {"reason": "left"},
                "src/beta.py::owned": {"reason": "right"},
            }
    audit = json.loads(_git(repo, "show", f"{tree}:tests/fixtures/contracts/"
                            "generated-command-audit.json").stdout)
    assert audit["alpha"]["source_digest"] == "left"
    assert audit["beta"]["source_digest"] == "right"
    assert all(row["review_notes"] == "keep approval" for row in audit.values())
    tests = _git(repo, "show", f"{tree}:tests/test_command_descriptors.py").stdout
    assert "assert 'left'" in tests and "assert 'right'" in tests
    assert await merge_generated_tree(GitManager(), repo, args) == tree
    assert _git(repo, "status", "--porcelain").stdout == ""


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


def _catalogue_branches(
    tmp_path, *, base_modules=("a",), other_module="b", current_module="c", all_generated=False,
    edit_existing=False,
):
    """Two branches that run the actual catalogue generator on different modules."""
    repo = tmp_path / "catalogue-repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "--initial-branch=main")
    # Callers also commit through helpers that scrub ambient identity variables.
    _git(repo, "config", "user.name", "Tester")
    _git(repo, "config", "user.email", "tester@example.test")
    for name in ("catalogue.py", "discovery.py"):
        target = repo / "src" / "test_selection" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / "src" / "test_selection" / name, target)
    for package in (repo / "src", repo / "src" / "test_selection"):
        (package / "__init__.py").touch()
    scripts = repo / "scripts"
    scripts.mkdir()
    shutil.copyfile(ROOT / "scripts" / "generate-selection-catalogue.py",
                    scripts / "generate-selection-catalogue.py")
    regenerator = scripts / "regenerate-generated.sh"
    regenerator.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\npython3 scripts/generate-selection-catalogue.py\n"
    )
    regenerator.chmod(0o755)
    shutil.copyfile(ROOT / ".gitattributes", repo / ".gitattributes")
    if all_generated:
        for path in _listed():
            source, destination = ROOT / path, repo / path
            if source.is_dir():
                shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
    (repo / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (repo / "tests").mkdir(exist_ok=True)
    (repo / cat.AREAS_PATH).write_text(
        'version: 1\nareas:\n  - id: area\n    description: Tests.\n'
        '    match: ["tests/test_*.py"]\n'
    )

    def add_module(name, imported=""):
        content = f"import src.{imported}\n" if imported else ""
        (repo / "tests" / f"test_{name}.py").write_text(
            content + f"def test_{name}():\n    pass\n"
        )
        subprocess.run(
            [sys.executable, str(scripts / "generate-selection-catalogue.py")],
            cwd=repo, capture_output=True, text=True, check=True,
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", name)
        return _git(repo, "rev-parse", "HEAD").stdout.strip()

    for module in base_modules:
        base = add_module(module)
    _git(repo, "switch", "-qc", "other")
    other = add_module(base_modules[0], "other") if edit_existing else add_module(other_module)
    _git(repo, "switch", "-q", "main")
    current = (
        add_module(base_modules[-1], "current") if edit_existing else add_module(current_module)
    )
    return repo, base, current, other


@pytest.mark.parametrize("edit_existing", [False, True], ids=["additions", "imports"])
def test_independent_test_changes_merge_all_generated_files_with_plain_git(tmp_path, edit_existing):
    # Keep the insertions in distinct sorted positions, including the same
    # area's module index. Adjacent insertions can still be real text conflicts.
    repo, _, _, _ = _catalogue_branches(
        tmp_path, base_modules=("00", "20", "40", "60", "80"),
        other_module="10", current_module="70", all_generated=True,
        edit_existing=edit_existing,
    )
    assert _git(repo, "config", "--get", "merge.aq-generated.driver", check=False).returncode == 1
    result = _git(repo, "merge", "--no-edit", "other", check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(repo, "diff", "--name-only", "--diff-filter=U").stdout == ""
    expected = cat.build_catalogue(repo, cat.load_areas(repo / cat.AREAS_PATH))
    actual = (repo / cat.CATALOGUE_PATH).read_text()
    assert actual == cat.render_catalogue(expected)
    assert cat.load_catalogue(repo / cat.CATALOGUE_PATH) == expected
    assert "digest" not in json.loads(actual)
    # Test-only inputs change only the selection catalogue; every other audited
    # generated output remains byte-identical through the plain merge.
    for path in _listed():
        if path == cat.CATALOGUE_PATH:
            continue
        source = ROOT / path
        files = sorted(source.rglob("*")) if source.is_dir() else [source]
        for file in files:
            if file.is_file() and "__pycache__" not in file.parts:
                assert (repo / file.relative_to(ROOT)).read_bytes() == file.read_bytes()


async def test_generated_catalogue_conflict_rebuilds_both_branches(tmp_path):
    repo, base, current, other = _catalogue_branches(tmp_path)
    args = ["merge-tree", "--write-tree", f"--merge-base={base}", current, other]
    assert _git(repo, *args, check=False).returncode == 1
    tree = await merge_generated_tree(GitManager(), repo, args)
    actual = _git(repo, "show", f"{tree}:{cat.CATALOGUE_PATH}").stdout
    merged = tmp_path / "merged"
    merged.mkdir()
    # The canonical catalogue is computed from the union of real module inputs.
    shutil.copytree(repo / "tests", merged / "tests")
    shutil.copyfile(repo / "pyproject.toml", merged / "pyproject.toml")
    (merged / "tests" / "test_b.py").write_text("def test_b():\n    pass\n")
    expected = cat.render_catalogue(cat.build_catalogue(merged, cat.load_areas(merged / cat.AREAS_PATH)))
    assert actual == expected
    assert set(json.loads(actual)["modules"]) == {
        "tests/test_a.py", "tests/test_b.py", "tests/test_c.py",
    }
    assert await merge_generated_tree(GitManager(), repo, args) == tree
    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == current
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert not list(tmp_path.glob("aq-regen-*"))
    assert len(_git(repo, "worktree", "list", "--porcelain").stdout.split("worktree ")) == 2


@pytest.mark.parametrize("failure", ["source", "failed", "stray"])
async def test_generated_regeneration_keeps_conflicts_on_failure(tmp_path, failure):
    repo, base, current, other = _catalogue_branches(tmp_path)
    command = "scripts/regenerate-generated.sh"
    if failure == "source":
        _git(repo, "switch", "-q", "other")
        (repo / "shared.txt").write_text("theirs\n")
        _git(repo, "add", "shared.txt")
        _git(repo, "commit", "-qm", "source conflict")
        other = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "switch", "-q", "main")
        (repo / "shared.txt").write_text("ours\n")
        _git(repo, "add", "shared.txt")
        _git(repo, "commit", "-qm", "source conflict")
        current = _git(repo, "rev-parse", "HEAD").stdout.strip()
    else:
        script = tmp_path / "regenerate.py"
        script.write_text(
            "raise SystemExit(1)\n" if failure == "failed" else
            "from pathlib import Path\nPath('shared.txt').write_text('unexpected')\n"
        )
        command = f"{sys.executable} {script}"
    with pytest.raises(GeneratedMergeConflict) as caught:
        await merge_generated_tree(
            GitManager(), repo,
            ["merge-tree", "--write-tree", f"--merge-base={base}", current, other],
            command=command,
        )
    expected_error = "exited 1" if failure == "failed" else "shared.txt"
    assert expected_error in str(caught.value)
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert not list(tmp_path.glob("aq-regen-*"))


async def test_clean_generated_overlap_is_regenerated_and_deleted_side_is_supported(tmp_path):
    repo, base, current, other = _catalogue_branches(tmp_path)
    # Even a driver-resolved merge is rebuilt, and a modify/delete overlap uses
    # the canonical generator rather than needing a text merge driver.
    for delete in (False, True):
        if delete:
            _git(repo, "rm", cat.CATALOGUE_PATH)
            _git(repo, "commit", "-qm", "delete catalogue")
            current = _git(repo, "rev-parse", "HEAD").stdout.strip()
        args = [*([] if delete else GENERATED_MERGE_CONFIG), "merge-tree", "--write-tree",
                f"--merge-base={base}", current, other]
        assert _git(repo, *args, check=False).returncode == int(delete)
        tree = await merge_generated_tree(GitManager(), repo, args)
        actual = json.loads(_git(repo, "show", f"{tree}:{cat.CATALOGUE_PATH}").stdout)
        assert set(actual["modules"]) == {
            "tests/test_a.py", "tests/test_b.py", "tests/test_c.py",
        }


@pytest.mark.parametrize("failure", ["exit", "timeout", "missing"])
async def test_clean_merge_regeneration_infrastructure_failure_is_retryable(tmp_path, failure):
    repo, base, current, other = _catalogue_branches(tmp_path)
    args = [*GENERATED_MERGE_CONFIG, "merge-tree", "--write-tree",
            f"--merge-base={base}", current, other]
    assert _git(repo, *args, check=False).returncode == 0
    script = tmp_path / "regenerate.py"
    script.write_text(
        "raise SystemExit(1)\n" if failure == "exit" else "import time\ntime.sleep(30)\n"
    )
    command = (
        str(tmp_path / "absent-regenerator") if failure == "missing"
        else f"{sys.executable} {script}"
    )
    # A clean merge has no conflict: the failure must not become GeneratedMergeConflict.
    with pytest.raises(GitError, match="generated regeneration failed"):
        await merge_generated_tree(
            GitManager(), repo, args, command=command,
            timeout_seconds=1 if failure == "timeout" else 600,
        )
    assert _git(repo, "status", "--porcelain").stdout == ""
    assert not list(tmp_path.glob("aq-regen-*"))


async def test_generated_fallback_preserves_the_explicit_merge_base(tmp_path):
    repo, base, current, other = _catalogue_branches(tmp_path)
    _git(repo, "switch", "-qc", "inherited", base)
    (repo / "shared.txt").write_text("inherited\n")
    _git(repo, "add", "shared.txt")
    _git(repo, "commit", "-qm", "inherited source")
    inherited = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # Both sides inherit the same new source, then one edits it again. An
    # inferred base merges cleanly; the requested older base must report add/add.
    for branch in ("main", "other"):
        _git(repo, "switch", "-q", branch)
        _git(repo, "merge", "--no-edit", inherited)
        if branch == "main":
            (repo / "shared.txt").write_text("current\n")
            _git(repo, "commit", "-qam", "current source")
        if branch == "main":
            current = _git(repo, "rev-parse", "HEAD").stdout.strip()
        else:
            other = _git(repo, "rev-parse", "HEAD").stdout.strip()
    with pytest.raises(GeneratedMergeConflict, match="shared.txt"):
        await merge_generated_tree(
            GitManager(), repo,
            ["merge-tree", "--write-tree", f"--merge-base={base}", current, other],
        )


@pytest.mark.slow
def test_committed_generated_files_match_regeneration():
    """The one-command drift check: every generator, run in a scratch copy."""
    result = subprocess.run(
        [str(SCRIPT), "--check"], capture_output=True, text=True, timeout=900, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
