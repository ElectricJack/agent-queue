"""The smart-test-selection artifacts: areas, generated catalogue, rules and policy.

Ratchets over the committed files (the catalogue regenerates byte for byte,
the rules map every tracked path, the policy pins the spec's thresholds) plus
the loaders' refusals on small synthetic trees.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.test_selection import catalogue as cat
from src.test_selection import discovery

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _pg_backend():
    """Pure file inspection; never allocate a database."""


def _tracked(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, text=True, check=True
    ).stdout
    return [path for path in out.split("\0") if path]


def _project(tmp_path: Path, files: dict[str, str]) -> Path:
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    for rel, text in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return tmp_path


def _write_catalogue(root: Path, areas: list[cat.AreaSpec]) -> cat.Catalogue:
    catalogue = cat.build_catalogue(root, areas)
    (root / cat.CATALOGUE_PATH).write_text(cat.render_catalogue(catalogue))
    return catalogue


# --------------------------------------------------------------------------
# The committed artifacts


@pytest.mark.parametrize(
    "pattern,path,expected",
    [
        ("tests/test_claim_*.py", "tests/test_claim_queries.py", True),
        ("tests/test_claim_*.py", "tests/sub/test_claim_queries.py", False),
        ("migrations/**", "migrations/versions/a00000000025_x.py", True),
        ("src/**/*.py", "src/a.py", True),
        ("*.md", "README.md", True),
        ("*.md", "docs/README.md", False),
        ("docs/**/*.md", "docs/guides/x.md", True),
        ("tests/perf", "tests/perf/conftest.py", False),
        ("tests/test_?.py", "tests/test_a.py", True),
        ("tests/test_?.py", "tests/test_ab.py", False),
        ("src/a+b.py", "src/a+b.py", True),
    ],
)
def test_match_glob(pattern, path, expected):
    assert cat.match_glob(pattern, path) is expected


def test_committed_catalogue_matches_regeneration():
    areas = cat.load_areas(ROOT / cat.AREAS_PATH)
    expected = cat.render_catalogue(cat.build_catalogue(ROOT, areas))
    committed = (ROOT / cat.CATALOGUE_PATH).read_text(encoding="utf-8")
    assert committed == expected, (
        "selection catalogue drifted; run `python scripts/generate-selection-catalogue.py`"
    )


def test_every_runnable_module_is_catalogued_and_exists():
    catalogue = cat.load_catalogue(ROOT / cat.CATALOGUE_PATH)
    assert cat.validate_catalogue(ROOT, catalogue) == []
    _, roots = discovery.pytest_rootdir(ROOT)
    assert set(discovery.relative_modules(ROOT, roots)) == set(catalogue.universe)
    assert catalogue.roots == ("tests",)
    assert "tests/test_cli_test_runner.py" in catalogue.universe
    assert "tests/perf/conftest.py" not in catalogue.universe
    assert catalogue.areas_for_module("tests/test_selection_catalogue.py")


def test_rules_name_only_known_modules_and_areas_and_cover_the_tree():
    catalogue = cat.load_catalogue(ROOT / cat.CATALOGUE_PATH)
    rules = cat.load_rules(ROOT / cat.RULES_PATH, catalogue)
    assert "pyproject.toml" in rules.global_invalidators
    assert "tests/conftest.py" in rules.global_invalidators
    assert "tests/test_selection_catalogue.py" in rules.critical
    assert cat.validate_rules_cover_tree(ROOT, rules, catalogue, _tracked(ROOT)) == []


def test_policy_pins_the_spec_thresholds():
    policy = cat.load_policy(ROOT / cat.POLICY_PATH)
    assert (policy.model, policy.question_schema_version) == ("jev-1.13.0", 1)
    assert (policy.omit_choice, policy.omit_min_probability, policy.omit_min_confidence) == (
        "unaffected",
        0.98,
        0.90,
    )
    assert policy.digest == cat.file_digest(ROOT / cat.POLICY_PATH)


def test_runner_helpers_are_the_discovery_helpers():
    from src.cli import test_runner

    assert test_runner._pytest_rootdir is discovery.pytest_rootdir
    assert test_runner._test_modules is discovery.test_modules
    assert test_runner._SKIP_DIRS is discovery.SKIP_DIRS


# --------------------------------------------------------------------------
# Generation


def test_generator_refuses_an_unmatched_module_and_an_empty_area(tmp_path):
    root = _project(
        tmp_path,
        {
            "tests/test_a.py": '"""Alpha."""\nimport pytest\nfrom src.pkg.a import alpha\n',
            "tests/test_b.py": "",
        },
    )
    areas = [
        cat.AreaSpec("alpha", "Alpha things.", ("tests/test_a.py",)),
        cat.AreaSpec("ghost", "Nothing matches.", ("tests/test_zzz*.py",)),
    ]
    with pytest.raises(cat.CatalogueError) as excinfo:
        cat.build_catalogue(root, areas)
    problems = "\n".join(excinfo.value.problems)
    assert "tests/test_b.py" in problems and "ghost" in problems


def test_metadata_is_extracted_deterministically(tmp_path):
    root = _project(
        tmp_path,
        {
            "tests/test_a.py": (
                '"""Alpha budgets.\n\nmore"""\nimport pytest\nfrom src.pkg.a import alpha\n'
                "import src.pkg.b\n"
                "pytestmark = pytest.mark.perf\n\n@pytest.mark.slow\ndef test_x():\n    pass\n"
            )
        },
    )
    areas = [cat.AreaSpec("alpha", "A.", ("tests/*.py",))]
    catalogue = cat.build_catalogue(root, areas)
    info = catalogue.modules["tests/test_a.py"]
    assert info.imports == ("src.pkg.a", "src.pkg.b")
    assert info.markers == ("perf", "slow")
    assert info.summary == "Alpha budgets."
    assert info.default_arm is False
    assert catalogue.module_for_import("src.pkg.a") == "src/pkg/a.py"
    assert catalogue.module_for_import("json") is None
    twice = cat.build_catalogue(root, areas)
    assert catalogue.digest == twice.digest and catalogue.digest.startswith("sha256:")
    assert cat.render_catalogue(catalogue) == cat.render_catalogue(twice)


def test_imports_include_function_local_ones_and_submodules_named_in_a_from_import(tmp_path):
    root = _project(
        tmp_path,
        {
            "src/__init__.py": "",
            "src/pkg/__init__.py": "",
            "src/pkg/sub.py": "",
            "tests/test_a.py": (
                "from src.pkg import sub, VALUE\n"
                "from . import helper\n"
                "def test_x():\n    from src.other import thing\n    import src.late\n"
            ),
        },
    )
    catalogue = cat.build_catalogue(root, [cat.AreaSpec("alpha", "A.", ("tests/*.py",))])
    info = catalogue.modules["tests/test_a.py"]
    assert info.imports == ("src.late", "src.other", "src.pkg", "src.pkg.sub")
    assert info.default_arm is True and info.summary == ""


def test_dotted_name_maps_source_paths():
    assert cat.dotted_name("src/pkg/a.py") == "src.pkg.a"
    assert cat.dotted_name("src/pkg/__init__.py") == "src.pkg"
    assert cat.dotted_name("src/prompts/x.md") is None
    assert cat.dotted_name("src/my-dir/a.py") is None


def test_load_catalogue_refuses_a_hand_edit_and_validate_names_the_drift(tmp_path):
    root = _project(tmp_path, {"tests/test_a.py": '"""Alpha."""\n'})
    areas_yaml = (
        'version: 1\nareas:\n  - id: alpha\n    description: "A."\n    match: ["tests/*.py"]\n'
    )
    (root / cat.AREAS_PATH).write_text(areas_yaml)
    catalogue = _write_catalogue(root, cat.load_areas(root / cat.AREAS_PATH))
    assert cat.load_catalogue(root / cat.CATALOGUE_PATH) == catalogue
    assert cat.validate_catalogue(root, catalogue) == []

    data = json.loads((root / cat.CATALOGUE_PATH).read_text())
    data["modules"]["tests/test_a.py"]["summary"] = "Edited."
    (root / cat.CATALOGUE_PATH).write_text(json.dumps(data))
    with pytest.raises(cat.CatalogueError, match="digest mismatch"):
        cat.load_catalogue(root / cat.CATALOGUE_PATH)

    (root / "tests/test_new.py").write_text("")
    problems = cat.validate_catalogue(root, catalogue)
    assert any("tests/test_new.py: runnable but not catalogued" in p for p in problems)
    (root / "tests/test_a.py").unlink()
    problems = cat.validate_catalogue(root, catalogue)
    assert any("tests/test_a.py: catalogued but not a runnable module" in p for p in problems)


def test_load_catalogue_refuses_another_generator_version(tmp_path):
    root = _project(tmp_path, {"tests/test_a.py": ""})
    _write_catalogue(root, [cat.AreaSpec("alpha", "A.", ("tests/*.py",))])
    data = json.loads((root / cat.CATALOGUE_PATH).read_text())
    data["generator_version"] = cat.GENERATOR_VERSION + 1
    (root / cat.CATALOGUE_PATH).write_text(json.dumps(data))
    with pytest.raises(cat.CatalogueError, match="generator_version"):
        cat.load_catalogue(root / cat.CATALOGUE_PATH)


def test_catalogue_identity_is_computed_without_a_committed_global_digest(tmp_path):
    root = _project(tmp_path, {"tests/test_a.py": '"""Alpha."""\n'})
    areas = [cat.AreaSpec("alpha", "A.", ("tests/*.py",))]
    original = _write_catalogue(root, areas)
    data = json.loads(cat.render_catalogue(original))
    assert "digest" not in data
    assert data["modules"]["tests/test_a.py"]["digest"].startswith("sha256:")
    assert cat.load_catalogue(root / cat.CATALOGUE_PATH).digest == original.digest
    assert original.digest == cat.file_digest(root / cat.CATALOGUE_PATH)

    (root / "tests/test_b.py").write_text('"""Beta."""\n')
    added = _write_catalogue(root, areas)
    assert added.digest != original.digest
    assert json.loads(cat.render_catalogue(added))["modules"]["tests/test_a.py"] == (
        data["modules"]["tests/test_a.py"]
    )


@pytest.mark.parametrize("edited", ["description", "roots", "record_digest"])
def test_local_digests_protect_catalogue_context(tmp_path, edited):
    root = _project(tmp_path, {"tests/test_a.py": ""})
    catalogue = _write_catalogue(root, [cat.AreaSpec("alpha", "A.", ("tests/*.py",))])
    data = json.loads(cat.render_catalogue(catalogue))
    if edited == "description":
        data["areas"]["alpha"]["description"] = "Edited."
    elif edited == "roots":
        data["roots"] = ["other"]
    else:
        del data["modules"]["tests/test_a.py"]["digest"]
    with pytest.raises(cat.CatalogueError, match="digest mismatch"):
        cat.load_catalogue_text(json.dumps(data), source="edited catalogue")


def test_historical_v1_catalogue_retains_its_identity_and_integrity_checks(tmp_path):
    root = _project(tmp_path, {"tests/test_a.py": '"""Alpha."""\n'})
    catalogue = _write_catalogue(root, [cat.AreaSpec("alpha", "A.", ("tests/*.py",))])
    data = json.loads(cat.render_catalogue(catalogue))
    data.update(schema_version=1, generator_version=1, digest="")
    for body in data["modules"].values():
        del body["digest"]
    canonical = json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    data["digest"] = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    historical = json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"

    loaded = cat.load_catalogue_text(historical, source="base catalogue")
    assert loaded.digest == data["digest"]
    assert loaded.modules == catalogue.modules and loaded.areas == catalogue.areas
    assert cat.render_catalogue(loaded) == historical
    data["modules"]["tests/test_a.py"]["summary"] = "Edited."
    with pytest.raises(cat.CatalogueError, match="digest mismatch"):
        cat.load_catalogue_text(json.dumps(data), source="base catalogue")


@pytest.mark.parametrize(
    "body,fragment",
    [
        ('  - id: Alpha\n    description: "A."\n    match: ["x"]\n', "not kebab-case"),
        ('  - id: a\n    description: "A."\n    match: ["x"]\n' * 2, "duplicate id"),
        ('  - id: a\n    description: ""\n    match: ["x"]\n', "description"),
        ("  - id: a\n    description: A.\n    match: []\n", "must not be empty"),
        ('  - id: a\n    description: A.\n    match: ["x"]\n    extra: 1\n', "unknown key"),
        (f'  - id: {"a" * 41}\n    description: A.\n    match: ["x"]\n', "longer than 40"),
    ],
)
def test_load_areas_refuses_malformed_entries(tmp_path, body, fragment):
    path = tmp_path / "areas.yaml"
    path.write_text("version: 1\nareas:\n" + body)
    with pytest.raises(cat.CatalogueError) as excinfo:
        cat.load_areas(path)
    assert fragment in "\n".join(excinfo.value.problems)


def test_the_generator_script_writes_and_checks(tmp_path):
    root = _project(tmp_path, {"tests/test_a.py": ""})
    (root / cat.AREAS_PATH).write_text(
        'version: 1\nareas:\n  - id: alpha\n    description: "A."\n    match: ["tests/test_a.py"]\n'
    )
    script = ROOT / "scripts" / "generate-selection-catalogue.py"

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script), "--root", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    assert run("--check").returncode == 1
    assert run().returncode == 0
    checked = run("--check")
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert "1 modules in 1 areas" in checked.stdout
    (root / "tests/test_b.py").write_text("")
    refused = run()
    assert refused.returncode == 1 and "tests/test_b.py: matches no area" in refused.stderr


# --------------------------------------------------------------------------
# The named-module guard (tests/conftest.py refuses a stale entry up front)

_ALPHA_AREAS = (
    'version: 1\nareas:\n  - id: alpha\n    description: "A."\n    match: ["tests/test_a*.py"]\n'
)


def _catalogued_project(tmp_path: Path) -> Path:
    root = _project(tmp_path, {"tests/test_a.py": '"""Alpha."""\nimport pytest\n'})
    (root / cat.AREAS_PATH).write_text(_ALPHA_AREAS)
    _write_catalogue(root, cat.load_areas(root / cat.AREAS_PATH))
    return root


def test_named_test_modules_takes_files_and_node_ids_under_testpaths(tmp_path):
    root = _project(
        tmp_path,
        {
            "tests/test_a.py": "",
            "tests/sub/test_b.py": "",
            "tests/sub/c_test.py": "",
            "tests/helper.py": "",
            "tests/.hidden/test_d.py": "",
            "tests/node_modules/test_e.py": "",
            "scripts/test_f.py": "",
        },
    )
    args = [
        "tests/test_a.py::test_x[1-2]",
        str(root / "tests/sub/test_b.py"),
        "./tests/sub/c_test.py::Class::test_y",
        "tests/",
        "tests/sub",
        "tests/helper.py",
        "tests/test_missing.py",
        "tests/.hidden/test_d.py",
        "tests/node_modules/test_e.py",
        "scripts/test_f.py",
    ]
    assert discovery.named_test_modules(root, root, args) == [
        "tests/sub/c_test.py",
        "tests/sub/test_b.py",
        "tests/test_a.py",
    ]
    assert discovery.named_test_modules(root, root / "tests", ["test_a.py", "sub"]) == [
        "tests/test_a.py"
    ]


def test_stale_named_modules_is_quiet_for_current_entries(tmp_path):
    root = _catalogued_project(tmp_path)
    assert cat.stale_named_modules(root, ["tests/test_a.py"]) == []
    assert cat.stale_named_modules(root, []) == []


def test_stale_named_modules_names_an_uncatalogued_module_and_the_area_it_matches(tmp_path):
    root = _catalogued_project(tmp_path)
    (root / "tests/test_another.py").write_text("")
    assert cat.stale_named_modules(root, ["tests/test_a.py", "tests/test_another.py"]) == [
        f"tests/test_another.py: not in {cat.CATALOGUE_PATH} (area 'alpha' matches it)"
    ]


def test_stale_named_modules_names_a_module_no_area_matches(tmp_path):
    root = _catalogued_project(tmp_path)
    (root / "tests/test_b.py").write_text("")
    assert cat.stale_named_modules(root, ["tests/test_b.py"]) == [
        f"tests/test_b.py: matches no area in {cat.AREAS_PATH}; add it to an area's `match` globs"
    ]


def test_stale_named_modules_names_each_drifted_field(tmp_path):
    root = _catalogued_project(tmp_path)
    (root / "tests/test_a.py").write_text(
        '"""Alpha, edited."""\nimport pytest\nfrom src.pkg import thing\n\n'
        "@pytest.mark.slow\ndef test_x():\n    pass\n"
    )
    assert cat.stale_named_modules(root, ["tests/test_a.py"]) == [
        "tests/test_a.py: catalogue entry is stale (imports, markers, summary)"
    ]


def test_stale_named_modules_sees_an_area_change(tmp_path):
    root = _catalogued_project(tmp_path)
    (root / cat.AREAS_PATH).write_text(
        _ALPHA_AREAS + '  - id: beta\n    description: "B."\n    match: ["tests/*.py"]\n'
    )
    assert cat.stale_named_modules(root, ["tests/test_a.py"]) == [
        "tests/test_a.py: catalogue entry is stale (areas)"
    ]


@pytest.mark.parametrize(
    "path,text",
    [
        (cat.CATALOGUE_PATH, None),
        (cat.CATALOGUE_PATH, "{not json"),
        (cat.CATALOGUE_PATH, '{"modules": []}'),
        (cat.CATALOGUE_PATH, '{"schema_version": 0, "generator_version": 0, "modules": {}}'),
        (cat.AREAS_PATH, None),
        (cat.AREAS_PATH, "version: 1\nareas: []\n"),
    ],
)
def test_stale_named_modules_leaves_whole_file_faults_to_the_ratchet(tmp_path, path, text):
    root = _catalogued_project(tmp_path)
    (root / "tests/test_b.py").write_text("")
    if text is None:
        (root / path).unlink()
    else:
        (root / path).write_text(text)
    assert cat.stale_named_modules(root, ["tests/test_b.py"]) == []


def test_stale_named_modules_message_says_what_to_run_and_commit():
    message = cat.stale_named_modules_message(["tests/test_b.py: drifted"])
    assert "  - tests/test_b.py: drifted" in message
    assert "python scripts/generate-selection-catalogue.py" in message
    assert cat.CATALOGUE_PATH in message and cat.AREAS_PATH in message
    assert "tests/test_generated_artifacts.py" in message


def test_conftest_refuses_a_named_module_with_a_stale_entry(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from tests.conftest import _refuse_stale_selection_catalogue

    monkeypatch.delenv("CI", raising=False)
    root = _catalogued_project(tmp_path)
    (root / "tests/test_another.py").write_text("")

    def config(*args: str, **extra) -> SimpleNamespace:
        return SimpleNamespace(
            rootpath=root,
            invocation_params=SimpleNamespace(dir=root),
            args=list(args),
            option=SimpleNamespace(pyargs=extra.pop("pyargs", False)),
            **extra,
        )

    with pytest.raises(pytest.UsageError, match="tests/test_another.py: not in"):
        _refuse_stale_selection_catalogue(config("tests/test_another.py::test_x"))
    _refuse_stale_selection_catalogue(config("tests/"))
    _refuse_stale_selection_catalogue(config("tests/test_a.py"))
    _refuse_stale_selection_catalogue(config("tests/test_another.py", workerinput={}))
    _refuse_stale_selection_catalogue(config("tests/test_another.py", pyargs=True))
    monkeypatch.setenv("CI", "true")
    _refuse_stale_selection_catalogue(config("tests/test_another.py"))


# --------------------------------------------------------------------------
# Rules and policy


RULES = """\
version: 1
global_invalidators: ["pyproject.toml", "tests/conftest.py"]
scoped_conftests:
  - {path: "tests/sub/conftest.py", subtree: "tests/sub"}
non_behavioral: ["*.md"]
ownership:
  - {paths: ["src/pkg/a.py"], areas: ["alpha"]}
  - {paths: ["docs/**"], areas: ["beta"]}
source_scanning:
  - {modules: ["tests/test_a.py"], triggers: ["docs/**"]}
critical: ["tests/test_a.py"]
"""


@pytest.fixture
def small_repo(tmp_path):
    root = _project(
        tmp_path,
        {
            "tests/conftest.py": "",
            "tests/test_a.py": "from src.pkg.a import alpha\n",
            "tests/sub/conftest.py": "",
            "tests/sub/test_b.py": "import src.pkg.b\n",
            "src/pkg/a.py": "",
            "src/pkg/b.py": "",
            "docs/guide.md": "",
            "README.md": "",
        },
    )
    catalogue = _write_catalogue(
        root,
        [
            cat.AreaSpec("alpha", "Alpha.", ("tests/test_a.py",)),
            cat.AreaSpec("beta", "Beta.", ("tests/sub/*.py",)),
        ],
    )
    (root / cat.RULES_PATH).write_text(RULES)
    return root, catalogue


def test_rules_load_and_cover_a_small_tree(small_repo):
    root, catalogue = small_repo
    rules = cat.load_rules(root / cat.RULES_PATH, catalogue)
    assert rules.scoped_conftests == (cat.ScopedConftest("tests/sub/conftest.py", "tests/sub"),)
    assert rules.ownership[0] == cat.OwnershipRule(("src/pkg/a.py",), ("alpha",))
    assert rules.digest == cat.file_digest(root / cat.RULES_PATH)
    tracked = [
        "pyproject.toml",
        "README.md",
        "docs/guide.md",
        "src/pkg/a.py",
        "src/pkg/b.py",  # mapped by tests/sub/test_b.py's direct import
        "tests/conftest.py",
        "tests/sub/conftest.py",
        "tests/sub/test_b.py",
        "tests/test_a.py",
        cat.CATALOGUE_PATH,
    ]
    problems = cat.validate_rules_cover_tree(root, rules, catalogue, tracked)
    assert problems == [f"{cat.CATALOGUE_PATH}: no rule in {cat.RULES_PATH} maps it"]


def test_the_cover_check_reports_unmapped_shadowed_and_stale_rules(small_repo):
    root, catalogue = small_repo
    (root / cat.RULES_PATH).write_text(
        RULES.replace('["*.md"]', '["*.md", "docs/**/*.md"]').replace(
            '["src/pkg/a.py"]', '["src/pkg/a.py", "src/gone.py"]'
        )
    )
    rules = cat.load_rules(root / cat.RULES_PATH, catalogue)
    tracked = ["src/pkg/a.py", "src/pkg/data.json", "docs/guide.md", "tests/test_a.py"]
    problems = cat.validate_rules_cover_tree(root, rules, catalogue, tracked)
    assert f"src/pkg/data.json: no rule in {cat.RULES_PATH} maps it" in problems
    assert "docs/guide.md: non_behavioral shadows docs/**" in problems
    assert "ownership: path 'src/gone.py' matches no tracked file" in problems
    assert "global_invalidators: path 'pyproject.toml' matches no tracked file" in problems


def test_rules_refuse_unknown_modules_areas_and_keys(small_repo):
    root, catalogue = small_repo
    (root / cat.RULES_PATH).write_text(
        RULES.replace('areas: ["beta"]', 'areas: ["nope"]')
        .replace('critical: ["tests/test_a.py"]', 'critical: ["tests/test_gone.py"]')
        .replace("non_behavioral:", "non_behavioural:")
    )
    with pytest.raises(cat.CatalogueError) as excinfo:
        cat.load_rules(root / cat.RULES_PATH, catalogue)
    problems = "\n".join(excinfo.value.problems)
    assert "area 'nope' is not in the catalogue" in problems
    assert "tests/test_gone.py is not a catalogued module" in problems
    assert "unknown key(s) non_behavioural" in problems


POLICY = """\
version: 1
question_schema_version: 1
model: "jev-1.13.0"
omission:
  choice: unaffected
  min_probability: 0.98
  min_confidence: 0.90
"""


@pytest.mark.parametrize(
    "old,new,fragment",
    [
        ("choice: unaffected", "choice: affected", "must be 'unaffected'"),
        ("min_probability: 0.98", "min_probability: 1.5", "within [0, 1]"),
        ("min_confidence: 0.90", "min_confidence: yes", "must be a number"),
        ('model: "jev-1.13.0"', 'model: ""', "model must be"),
        ("version: 1\nquestion", "version: 2\nquestion", "version must be 1"),
    ],
)
def test_load_policy_refuses_what_the_spec_forbids(tmp_path, old, new, fragment):
    path = tmp_path / "policy.yaml"
    path.write_text(POLICY)
    assert cat.load_policy(path).omit_min_confidence == 0.90
    path.write_text(POLICY.replace(old, new))
    with pytest.raises(cat.CatalogueError) as excinfo:
        cat.load_policy(path)
    assert fragment in "\n".join(excinfo.value.problems)
