"""Per-test Bazel dependency discovery preserves runtime inputs without global fanout."""
import json
import os
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from scripts.bazel.test_dependencies import DependencyAnalyzer, discover

_GIT_IDENTITY = ["-c", "user.name=Tester", "-c", "user.email=tester@example.test"]


def _git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *_GIT_IDENTITY, *args], capture_output=True, text=True,
        check=check, env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    )


def test_repository_leaf_closure_avoids_database_bootstrap_fanout():
    root = Path(__file__).resolve().parents[1]
    result = discover(root, "tests/test_knowledge_inventory.py")
    assert "src/knowledge/imports/inventory.py" in result["source_files"]
    assert "tests/conftest.py" in result["test_sources"]
    assert not result["conservative"], result["conservative_reasons"]
    assert "src/database/adapters/postgresql.py" not in result["source_files"]
    assert "src/database/queries/job_queries.py" not in result["source_files"]
    assert not any(path.startswith("src/plugins/") for path in result["source_files"])


def write(root, path, source=""):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dedent(source), encoding="utf-8")


def test_import_closure_includes_package_initializers_and_relative_imports(tmp_path):
    write(tmp_path, "src/__init__.py", "from . import common")
    write(tmp_path, "src/common.py")
    write(tmp_path, "src/feature/__init__.py", "from . import setup")
    write(tmp_path, "src/feature/setup.py")
    write(tmp_path, "src/feature/main.py", "from . import helper")
    write(tmp_path, "src/feature/helper.py")
    write(tmp_path, "src/unrelated.py")
    write(tmp_path, "tests/test_sample.py", "from src.feature.main import run")
    result = discover(tmp_path, "tests/test_sample.py")
    assert result["source_files"] == [
        "src/__init__.py", "src/common.py", "src/feature/__init__.py",
        "src/feature/helper.py", "src/feature/main.py", "src/feature/setup.py",
    ]
    assert not result["conservative"]


def test_conftest_used_fixture_closure_hooks_and_autouse(tmp_path):
    for name in ("top", "hook", "automatic", "first", "second", "unused", "helper"):
        write(tmp_path, f"src/{name}.py")
    write(tmp_path, "tests/conftest.py", """
        import pytest
        import src.top
        def pytest_configure(config):
            import src.hook
        def helper():
            import src.helper
        @pytest.fixture(autouse=True)
        def automatic():
            import src.automatic
        @pytest.fixture
        def first(second):
            import src.first
            helper()
        @pytest.fixture
        def second():
            import src.second
        @pytest.fixture
        def unused():
            import src.unused
    """)
    write(tmp_path, "tests/test_sample.py", "def test_sample(first): pass")
    result = discover(tmp_path, "tests/test_sample.py")
    assert set(result["source_files"]) == {
        f"src/{name}.py" for name in ("top", "hook", "automatic", "first", "second", "helper")
    }
    assert "tests/conftest.py" in result["test_sources"]


def test_module_override_replaces_autouse_fixture_body(tmp_path):
    write(tmp_path, "src/unneeded.py")
    write(tmp_path, "tests/conftest.py", """
        import pytest
        @pytest.fixture(autouse=True)
        def connection():
            import src.unneeded
    """)
    write(tmp_path, "tests/test_sample.py", """
        import pytest
        @pytest.fixture
        def connection(): pass
        def test_sample(): pass
    """)
    assert discover(tmp_path, "tests/test_sample.py")["source_files"] == []


def test_usefixtures_and_literal_getfixturevalue(tmp_path):
    for name in ("marked", "requested", "unused"):
        write(tmp_path, f"src/{name}.py")
    write(tmp_path, "tests/conftest.py", """
        import pytest
        @pytest.fixture(name="marked_alias")
        def marked():
            import src.marked
        @pytest.fixture
        def requested():
            import src.requested
        @pytest.fixture
        def unused():
            import src.unused
    """)
    write(tmp_path, "tests/test_sample.py", """
        import pytest
        @pytest.mark.usefixtures("marked_alias")
        def test_sample(request):
            request.getfixturevalue("requested")
    """)
    result = discover(tmp_path, "tests/test_sample.py")
    assert result["source_files"] == ["src/marked.py", "src/requested.py"]
    assert not result["conservative"]


def test_dynamic_import_literal_and_alias_are_resolved(tmp_path):
    write(tmp_path, "src/selected.py")
    write(tmp_path, "src/unselected.py")
    write(tmp_path, "tests/test_sample.py", """
        from importlib import import_module as load
        def test_sample():
            load("src.selected")
            __import__("json")
    """)
    assert discover(tmp_path, "tests/test_sample.py")["source_files"] == ["src/selected.py"]


def test_unknown_dynamic_import_expands_conservatively(tmp_path):
    write(tmp_path, "src/one.py")
    write(tmp_path, "src/two.py")
    write(tmp_path, "tests/helper.py")
    write(tmp_path, "tests/test_sample.py", """
        import importlib
        def test_sample(name):
            importlib.import_module(name)
    """)
    result = discover(tmp_path, "tests/test_sample.py")
    assert result["source_files"] == ["src/one.py", "src/two.py"]
    assert "tests/helper.py" in result["test_sources"]
    assert result["conservative"]
    assert result["conservative_reasons"] == ["tests/test_sample.py: unresolved dynamic import"]


def test_tmp_path_reads_do_not_expand_sources_or_docs(tmp_path):
    write(tmp_path, "src/unrelated.py")
    write(tmp_path, "tests/test_sample.py", '''
        """The design is documented at docs/guides/example.md."""
        def test_sample(tmp_path):
            path = tmp_path / "output.txt"
            path.write_text("hello")
            assert path.read_text() == "hello"
            assert list(tmp_path.rglob("*.txt"))
    ''')
    result = discover(tmp_path, "tests/test_sample.py")
    assert not result["source_files"]
    assert not result["docs"]
    assert not result["conservative"]


def test_explicit_document_read_and_source_data_are_declared(tmp_path):
    write(tmp_path, "docs/guide.md", "guide")
    write(tmp_path, "src/template.txt", "template")
    write(tmp_path, "tests/test_sample.py", """
        from pathlib import Path
        def test_sample():
            assert Path("docs/guide.md").read_text()
            assert Path("src/template.txt").read_text()
    """)
    result = DependencyAnalyzer(tmp_path).discover(Path("tests/test_sample.py"))
    assert result["docs"]
    assert result["data_files"] == ["docs/guide.md", "src/template.txt"]


def test_imported_fixture_alias_keeps_parameter_dependencies(tmp_path):
    for name in ("imported", "dependency", "unused"):
        write(tmp_path, f"src/{name}.py")
    write(tmp_path, "tests/helpers.py", """
        import pytest
        @pytest.fixture
        def original(dependency):
            import src.imported
        @pytest.fixture
        def unused():
            import src.unused
    """)
    write(tmp_path, "tests/conftest.py", """
        import pytest
        @pytest.fixture
        def dependency():
            import src.dependency
    """)
    write(tmp_path, "tests/test_sample.py", """
        from tests.helpers import original as renamed
        def test_sample(renamed): pass
    """)
    result = discover(tmp_path, "tests/test_sample.py")
    assert result["source_files"] == ["src/dependency.py", "src/imported.py"]
    assert "tests/helpers.py" in result["test_sources"]


def test_pytest_plugin_fixtures_and_hooks(tmp_path):
    write(tmp_path, "src/hook.py")
    write(tmp_path, "src/fixture.py")
    write(tmp_path, "tests/plugin.py", """
        import pytest
        def pytest_configure(config):
            import src.hook
        @pytest.fixture
        def example():
            import src.fixture
    """)
    write(tmp_path, "tests/conftest.py", 'pytest_plugins = ["tests.plugin"]')
    write(tmp_path, "tests/test_sample.py", "def test_sample(example): pass")
    result = discover(tmp_path, "tests/test_sample.py")
    assert result["source_files"] == ["src/fixture.py", "src/hook.py"]


def test_production_runtime_imports_are_not_pruned(tmp_path):
    write(tmp_path, "src/entry.py", """
        def load():
            from src import runtime
    """)
    write(tmp_path, "src/runtime.py")
    write(tmp_path, "tests/test_sample.py", """
        import src.entry
        def test_sample():
            src.entry.load()
    """)
    assert discover(tmp_path, "tests/test_sample.py")["source_files"] == [
        "src/entry.py", "src/runtime.py",
    ]


def test_directory_data_excludes_build_metadata(tmp_path):
    write(tmp_path, "src/templates/example.md", "template")
    write(tmp_path, "src/templates/BUILD.bazel", "# generated")
    write(tmp_path, "tests/test_sample.py", """
        from pathlib import Path
        def test_sample():
            assert list(Path("src/templates").glob("*.md"))
    """)
    result = discover(tmp_path, "tests/test_sample.py")
    assert result["data_files"] == ["src/templates/example.md"]


def test_literal_data_excludes_worktree_control_files(tmp_path):
    write(tmp_path, ".git", "gitdir: /operator/checkout/.git/worktrees/worker")
    write(tmp_path, ".aq/claim.json", "{}")
    write(tmp_path, ".codex/hooks.json", "{}")
    write(tmp_path, ".pytest_cache/v/cache/lastfailed", "{}")
    write(tmp_path, ".aq-worktree.json", "{}")
    write(tmp_path, "tests/fixtures/input.txt", "declared input")
    write(tmp_path, "tests/test_sample.py", '''
        from pathlib import Path
        def test_sample():
            root = Path(__file__).resolve().parents[1]
            paths = [".git", ".aq/claim.json", "tests/fixtures/input.txt"]
            controls = [".codex/hooks.json", ".pytest_cache/v/cache/lastfailed",
                        ".aq-worktree.json"]
            (root / ".codex" / "hooks.json").read_text()
            (root / ".pytest_cache" / "v" / "cache" / "lastfailed").read_text()
            (root / ".aq-worktree.json").read_text()
            assert controls
            assert paths
    ''')
    result = discover(tmp_path, "tests/test_sample.py")
    assert result["data_files"] == ["tests/fixtures/input.txt"]


def test_lazy_package_imports_only_when_attribute_requested(tmp_path):
    write(tmp_path, "src/commands/__init__.py", """
        from typing import TYPE_CHECKING
        if TYPE_CHECKING:
            from src.commands.handler import Handler
        def __getattr__(name):
            if name == "Handler":
                from src.commands.handler import Handler
                return Handler
            raise AttributeError(name)
    """)
    write(tmp_path, "src/commands/principal.py")
    write(tmp_path, "src/commands/handler.py")
    write(tmp_path, "tests/test_narrow.py", "from src.commands.principal import Principal")
    write(tmp_path, "tests/test_from.py", "from src.commands import Handler")
    write(tmp_path, "tests/test_attribute.py", """
        import src.commands as commands
        def test_sample():
            assert commands.Handler
    """)
    write(tmp_path, "tests/test_dynamic.py", """
        import src.commands as commands
        def test_sample(name):
            assert getattr(commands, name)
    """)
    analyzer = DependencyAnalyzer(tmp_path)
    assert analyzer.discover("tests/test_narrow.py")["source_files"] == [
        "src/commands/__init__.py", "src/commands/principal.py",
    ]
    for name in ("from", "attribute", "dynamic"):
        assert "src/commands/handler.py" in analyzer.discover(f"tests/test_{name}.py")["source_files"]


def test_class_fixtures_include_dependency_union_and_imported_aliases(tmp_path):
    for name in ("shared", "imported", "automatic", "first", "second", "unused"):
        write(tmp_path, f"src/{name}.py")
    write(tmp_path, "tests/conftest.py", """
        import pytest
        @pytest.fixture
        def shared():
            import src.shared
        @pytest.fixture
        def unused():
            import src.unused
    """)
    write(tmp_path, "tests/helpers.py", """
        import pytest
        @pytest.fixture
        def original(shared):
            import src.imported
    """)
    write(tmp_path, "tests/test_sample.py", """
        import pytest
        from tests.helpers import original as renamed
        class TestFirst:
            @pytest.fixture(autouse=True)
            def automatic(self, renamed):
                import src.automatic
            @pytest.fixture
            def local(self, shared):
                import src.first
            def test_sample(self, local): pass
        class TestSecond:
            @pytest.fixture
            def local(self, renamed):
                import src.second
            def test_sample(self, local): pass
    """)
    result = discover(tmp_path, "tests/test_sample.py")
    assert result["source_files"] == [
        "src/automatic.py", "src/first.py", "src/imported.py", "src/second.py", "src/shared.py",
    ]
    assert not result["conservative"]


def test_indirect_parametrization_declares_fixture_names(tmp_path):
    for name in ("first", "second", "unused"):
        write(tmp_path, f"src/{name}.py")
    write(tmp_path, "tests/conftest.py", """
        import pytest
        @pytest.fixture
        def first():
            import src.first
        @pytest.fixture
        def second():
            import src.second
        @pytest.fixture
        def unused():
            import src.unused
    """)
    write(tmp_path, "tests/test_sample.py", """
        import pytest
        @pytest.mark.parametrize("first", ["a"], indirect=True)
        def test_first(first): pass
        @pytest.mark.parametrize(["second", "value"], [("b", 1)], indirect=["second"])
        def test_second(second, value): pass
    """)
    result = discover(tmp_path, "tests/test_sample.py")
    assert result["source_files"] == ["src/first.py", "src/second.py"]
    assert not result["conservative"]


def test_dynamic_indirect_parametrization_includes_all_available_fixtures(tmp_path):
    write(tmp_path, "src/declared.py")
    write(tmp_path, "src/dynamic.py")
    write(tmp_path, "tests/conftest.py", """
        import pytest
        @pytest.fixture
        def declared():
            import src.declared
        @pytest.fixture
        def dynamic():
            import src.dynamic
    """)
    write(tmp_path, "tests/test_sample.py", """
        def pytest_generate_tests(metafunc):
            metafunc.parametrize(metafunc.fixturenames, [("a",)], indirect=True)
        def test_sample(declared): pass
    """)
    result = discover(tmp_path, "tests/test_sample.py")
    assert result["source_files"] == ["src/declared.py", "src/dynamic.py"]
    assert result["conservative_reasons"] == [
        "tests/test_sample.py: unresolved fixture request in parametrize",
    ]


def test_imported_fixture_explicit_name_survives_local_alias(tmp_path):
    write(tmp_path, "src/shared.py")
    write(tmp_path, "src/imported.py")
    write(tmp_path, "tests/conftest.py", """
        import pytest
        @pytest.fixture
        def shared():
            import src.shared
    """)
    write(tmp_path, "tests/helpers.py", """
        import pytest
        @pytest.fixture(name="public_name")
        def original(shared):
            import src.imported
    """)
    write(tmp_path, "tests/test_sample.py", """
        from tests.helpers import original as private_alias
        def test_sample(public_name): pass
    """)
    result = discover(tmp_path, "tests/test_sample.py")
    assert result["source_files"] == ["src/imported.py", "src/shared.py"]


def test_composed_repository_data_paths_follow_imported_readers(tmp_path):
    root = tmp_path
    write(root, "dashboard/src/deviceLocal.ts", "export const keys = {};\n")
    write(root, "dashboard/AGENTS.md", "Documented transport keys\n")
    for ignored in ("node_modules/package/index.js", "dist/bundle.js", "src/cache.tsbuildinfo"):
        write(root, "dashboard/" + ignored)
    write(root, "tests/read_dashboard.py", '''
        from pathlib import Path
        ROOT = Path(__file__).parents[1]
        DASHBOARD = ROOT / "dashboard"
        def contents():
            return (DASHBOARD / "src" / "deviceLocal.ts").read_text()
    ''')
    write(root, "tests/test_reader.py", '''
        from tests.read_dashboard import contents
        def test_contents(): assert contents()
    ''')
    result = discover(root, "tests/test_reader.py")
    assert "dashboard/src/deviceLocal.ts" in result["data_files"]
    assert "dashboard/AGENTS.md" not in result["data_files"]
    assert not any("node_modules" in path or "/dist/" in path
                   or path.endswith(".tsbuildinfo") for path in result["data_files"])
    assert not result["conservative"]


def test_literal_frontend_fixture_and_source_directory_are_inputs(tmp_path):
    write(tmp_path, "dashboard/layout-checks/fixtures/pane-frames.json", "{}")
    write(tmp_path, "src/inspected.py", "VALUE = 1")
    write(tmp_path, "tests/test_reader.py", '''
        from pathlib import Path
        ROOT = Path(__file__).parents[1]
        def test_contents():
            assert Path("dashboard/layout-checks/fixtures/pane-frames.json").read_text()
            assert list((ROOT / "src").rglob("*.py"))
    ''')
    result = discover(tmp_path, "tests/test_reader.py")
    assert set(result["data_files"]) == {
        "dashboard/layout-checks/fixtures/pane-frames.json", "src/inspected.py",
    }


def test_composed_data_paths_keep_subtree_without_bare_string_fanout(tmp_path):
    write(tmp_path, "src/database/schema.py", "SCHEMA = 1")
    write(tmp_path, "src/unrelated.py", "VALUE = 1")
    write(tmp_path, "tests/unrelated.py", "VALUE = 1")
    write(tmp_path, "tests/fixtures/knowledge/export.json", "{}")
    write(tmp_path, "tests/test_reader.py", '''
        from pathlib import Path
        ROOT = Path(__file__).parents[1]
        module = "src"
        tests_dir = "tests"
        def test_contents():
            assert "src/example.py".startswith("src/")
            assert "tests/example.py".removeprefix("tests/")
            assert "src/" in ("src/", "tests/")
            assert "tests/" == "tests/"
            assert (ROOT / "src" / "database" / "schema.py").read_text()
            assert (Path(__file__).parent / "fixtures" / "knowledge" / "export.json").read_text()
    ''')
    result = discover(tmp_path, "tests/test_reader.py")
    assert result["data_files"] == [
        "src/database/schema.py", "tests/fixtures/knowledge/export.json",
    ]



def test_declared_subprocess_module_includes_transitive_imports_and_initializers(tmp_path):
    write(tmp_path, "src/__init__.py")
    write(tmp_path, "src/tool/__init__.py", "from . import bootstrap")
    write(tmp_path, "src/tool/bootstrap.py")
    write(tmp_path, "src/tool/entry.py", "from . import helper")
    write(tmp_path, "src/tool/helper.py", "from . import runtime")
    write(tmp_path, "src/tool/runtime.py")
    write(tmp_path, "src/unrelated.py")
    write(tmp_path, "tests/test_sample.py", """
        import subprocess
        import sys
        def test_sample():
            subprocess.run([sys.executable, "-m", "src.tool.entry"], check=True)
    """)
    write(tmp_path, "scripts/bazel/test_dependency_inputs/tests/test_sample.py.json", json.dumps(
        {"imports": ["src.tool.entry"]},
    ))

    result = discover(tmp_path, "tests/test_sample.py")

    assert result["source_files"] == [
        "src/__init__.py", "src/tool/__init__.py", "src/tool/bootstrap.py",
        "src/tool/entry.py", "src/tool/helper.py", "src/tool/runtime.py",
    ]
    assert not result["conservative"]


def test_declared_inputs_preserve_discovered_imports_fixtures_and_resources(tmp_path):
    for name in ("direct", "fixture", "declared", "unrelated"):
        write(tmp_path, f"src/{name}.py")
    write(tmp_path, "src/template.txt", "discovered resource")
    write(tmp_path, "tests/fixtures/contract.json", "{}")
    write(tmp_path, "tests/conftest.py", """
        import pytest
        @pytest.fixture
        def example():
            import src.fixture
    """)
    write(tmp_path, "tests/test_sample.py", """
        from pathlib import Path
        import src.direct
        def test_sample(example):
            assert Path("src/template.txt").read_text()
    """)
    write(tmp_path, "tests/test_other.py", "def test_other(): pass")
    write(tmp_path, "scripts/bazel/test_dependency_inputs/tests/test_sample.py.json", json.dumps({
        "imports": ["src.declared"],
        "data": ["tests/fixtures/*.json"],
    }))

    analyzer = DependencyAnalyzer(tmp_path)
    result = analyzer.discover("tests/test_sample.py")

    assert result["source_files"] == ["src/declared.py", "src/direct.py", "src/fixture.py"]
    assert result["data_files"] == ["src/template.txt", "tests/fixtures/contract.json"]
    assert "tests/conftest.py" in result["test_sources"]
    assert not result["conservative"]
    other = analyzer.discover("tests/test_other.py")
    assert other["source_files"] == []
    assert other["data_files"] == []


def test_declared_data_glob_tracks_added_and_deleted_members(tmp_path):
    write(tmp_path, "tests/test_sample.py", "def test_sample(): pass")
    write(tmp_path, "tests/fixtures/first.json", "{}")
    write(tmp_path, "tests/fixtures/readme.txt", "unmatched")
    write(tmp_path, "scripts/bazel/test_dependency_inputs/tests/test_sample.py.json", json.dumps(
        {"data": ["tests/fixtures/**/*.json"]},
    ))

    before = discover(tmp_path, "tests/test_sample.py")
    write(tmp_path, "tests/fixtures/nested/second.json", "{}")
    added = discover(tmp_path, "tests/test_sample.py")
    (tmp_path / "tests/fixtures/first.json").unlink()
    deleted = discover(tmp_path, "tests/test_sample.py")

    assert before["data_files"] == ["tests/fixtures/first.json"]
    assert added["data_files"] == [
        "tests/fixtures/first.json", "tests/fixtures/nested/second.json",
    ]
    assert deleted["data_files"] == ["tests/fixtures/nested/second.json"]


@pytest.mark.parametrize(
    ("declaration", "diagnostic"),
    [
        ({"imports": ["src.missing"]}, "unknown declared import src.missing"),
        ({"data": ["tests/fixtures/*.json"]}, "declared data matches no files"),
        ({"nonsense": "x"}, "unknown declaration keys"),
        ({"mode": "legacy"}, "mode: legacy requires reason"),
        ({"docs": False}, "docs: false requires reason"),
    ],
    ids=[
        "unknown-import", "unmatched-data-glob", "unknown-declaration-key",
        "mode-legacy-requires-reason", "docs-false-requires-reason",
    ],
)
def test_invalid_declared_inputs_fail_instead_of_silently_narrowing(
    tmp_path, declaration, diagnostic,
):
    write(tmp_path, "src/available.py")
    write(tmp_path, "tests/test_sample.py", "import src.available")
    write(
        tmp_path, "scripts/bazel/test_dependency_inputs/tests/test_sample.py.json",
        json.dumps(declaration),
    )

    with pytest.raises(ValueError, match=diagnostic):
        discover(tmp_path, "tests/test_sample.py")


def test_getattr_literal_attribute_retains_only_named_definition(tmp_path):
    """S6: a literal ``getattr(module, "name")`` activates exactly that
    top-level definition -- a sibling definition never named this way, and
    its own lazy import, stay unreached."""
    write(tmp_path, "src/registry.py", """
        def worker_one():
            from src import dep_one
            return dep_one

        def worker_two():
            from src import dep_two
            return dep_two
    """)
    write(tmp_path, "src/dep_one.py")
    write(tmp_path, "src/dep_two.py")
    write(tmp_path, "tests/test_sample.py", """
        import src.registry as registry
        def test_sample():
            assert getattr(registry, "worker_one")
    """)

    result = discover(tmp_path, "tests/test_sample.py")

    assert "src/dep_one.py" in result["source_files"]
    assert "src/dep_two.py" not in result["source_files"]
    assert not result["conservative"], result["conservative_reasons"]


def test_dunder_all_wildcard_import_activates_only_listed_names(tmp_path):
    """S6 soundness guard: ``__all__`` with ``from x import *`` activates the
    listed names -- a sibling definition left out of ``__all__`` stays
    unreached even though the wildcard import pulls in the module."""
    write(tmp_path, "src/toolkit.py", """
        __all__ = ["helper_a"]

        def helper_a():
            from src import dep_a
            return dep_a

        def helper_b():
            from src import dep_b
            return dep_b
    """)
    write(tmp_path, "src/dep_a.py")
    write(tmp_path, "src/dep_b.py")
    write(tmp_path, "tests/test_sample.py", """
        from src.toolkit import *
        def test_sample():
            pass
    """)

    result = discover(tmp_path, "tests/test_sample.py")

    assert "src/dep_a.py" in result["source_files"]
    assert "src/dep_b.py" not in result["source_files"]
    assert not result["conservative"], result["conservative_reasons"]


def test_declared_plugin_loader_resolves_suppress_dynamic_and_file_import_reasons(tmp_path):
    """S acceptance: plugin declarations. ``pkgutil.iter_modules`` fan-out and
    ``spec_from_file_location`` on an operator-supplied path are both
    genuinely dynamic; a declared ``resolves`` keeps the graph narrow instead
    of falling back to the whole repository."""
    write(tmp_path, "src/plugins/internal/__init__.py", """
        import importlib
        import pkgutil
        package = importlib.import_module(__name__)
        for importer, modname, ispkg in pkgutil.iter_modules(
            package.__path__, prefix=f"{__name__}.",
        ):
            importlib.import_module(modname)
    """)
    write(tmp_path, "src/plugins/internal/notes.py")
    write(tmp_path, "src/plugins/loader.py", """
        import importlib.util
        def load_plugin(path):
            spec = importlib.util.spec_from_file_location("plugin", path)
            return spec
    """)
    write(tmp_path, "tests/test_sample.py", """
        import src.plugins.internal
        import src.plugins.loader as loader
        def test_sample():
            loader.load_plugin("/operator/plugin.py")
    """)
    write(
        tmp_path, "scripts/bazel/test_dependency_inputs/src/plugins/internal/__init__.py.json",
        json.dumps({
            "import_globs": ["src/plugins/internal/*.py"],
            "resolves": ["src/plugins/internal/__init__.py: unresolved dynamic import"],
            "reason": "pkgutil.iter_modules walks submodules dynamically",
        }),
    )
    write(
        tmp_path, "scripts/bazel/test_dependency_inputs/src/plugins/loader.py.json",
        json.dumps({
            "resolves": ["src/plugins/loader.py: unresolved file import"],
            "reason": "spec_from_file_location loads an operator-supplied path outside the repository",
        }),
    )

    result = discover(tmp_path, "tests/test_sample.py")

    assert result["source_files"] == [
        "src/plugins/internal/__init__.py", "src/plugins/internal/notes.py",
        "src/plugins/loader.py",
    ]
    assert not result["conservative"], result["conservative_reasons"]


def test_check_stale_declarations_flags_unmatched_declared_resolves(tmp_path):
    """S acceptance: a stale ``resolves`` refusal. A declared reason that is
    never actually produced by a scan stays visible instead of silently
    rotting once the code it used to cover changes."""
    write(tmp_path, "src/plugins/loader.py", """
        import importlib.util
        def load_plugin(path):
            spec = importlib.util.spec_from_file_location("plugin", path)
            return spec
    """)
    write(tmp_path, "tests/test_sample.py", """
        import src.plugins.loader as loader
        def test_sample():
            loader.load_plugin("/operator/plugin.py")
    """)
    write(
        tmp_path, "scripts/bazel/test_dependency_inputs/src/plugins/loader.py.json",
        json.dumps({
            "resolves": [
                "src/plugins/loader.py: unresolved file import",
                "src/plugins/loader.py: this reason never happens",
            ],
            "reason": "spec_from_file_location loads an operator-supplied path outside the repository",
        }),
    )

    analyzer = DependencyAnalyzer(tmp_path)
    analyzer.discover("tests/test_sample.py")

    assert analyzer.check_stale_declarations() == {
        "src/plugins/loader.py": ["src/plugins/loader.py: this reason never happens"],
    }


def test_docs_false_override_suppresses_default_active_docs_literal(tmp_path):
    """A literal ``docs/...``-shaped string defaults to active for a tests/
    module; a declared ``docs: false`` (with its required reason) overrides
    that default instead of being ignored."""
    write(tmp_path, "tests/test_sample.py", """
        def test_sample():
            open("docs/guides/thing.md")
    """)
    write(
        tmp_path, "scripts/bazel/test_dependency_inputs/tests/test_sample.py.json",
        json.dumps({
            "docs": False,
            "reason": "this path merely starts with docs/ and is not documentation",
        }),
    )

    result = discover(tmp_path, "tests/test_sample.py")

    assert result["docs"] is False


def test_declared_broad_escape_hatch_forces_full_repository_fallback(tmp_path):
    """A declared ``broad`` reason is the explicit, visible escape hatch for
    genuinely unanalyzable execution: it forces the same whole-repository
    fallback as an unresolved dynamic import, with the declared text kept as
    the reason instead of a silent narrow (and wrong) graph."""
    write(tmp_path, "src/alpha.py")
    write(tmp_path, "src/unrelated.py")
    write(tmp_path, "tests/test_sample.py", "import src.alpha")
    write(
        tmp_path, "scripts/bazel/test_dependency_inputs/src/alpha.py.json",
        json.dumps({"broad": "legacy module wires itself up via exec()"}),
    )

    result = discover(tmp_path, "tests/test_sample.py")

    assert "src/unrelated.py" in result["source_files"]
    assert result["docs"] is True
    assert result["conservative"]
    assert "legacy module wires itself up via exec()" in result["conservative_reasons"]


def test_shard_per_module_merges_two_branches_without_conflict(tmp_path):
    """S5: one declaration shard per module means two branches that declare
    different modules touch different files, so a real merge never conflicts
    -- and ``repository_files()`` (``git ls-files --cached`` at a real root)
    picks up both shards from the merged index afterward."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "--initial-branch=main")
    write(repo, "src/alpha.py")
    write(repo, "src/beta.py")
    write(repo, "tests/test_alpha.py", "def test_alpha(): pass")
    write(repo, "tests/test_beta.py", "def test_beta(): pass")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")

    _git(repo, "checkout", "-qb", "one")
    write(
        repo, "scripts/bazel/test_dependency_inputs/tests/test_alpha.py.json",
        json.dumps({"imports": ["src.alpha"]}),
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "declare alpha")

    _git(repo, "checkout", "-q", "main")
    write(
        repo, "scripts/bazel/test_dependency_inputs/tests/test_beta.py.json",
        json.dumps({"imports": ["src.beta"]}),
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "declare beta")

    merged = _git(repo, "merge", "--no-edit", "one", check=False)
    assert merged.returncode == 0, merged.stdout + merged.stderr
    assert _git(repo, "diff", "--name-only", "--diff-filter=U").stdout == ""

    analyzer = DependencyAnalyzer(repo)
    assert analyzer.declarations["tests/test_alpha.py"] == {"imports": ["src.alpha"]}
    assert analyzer.declarations["tests/test_beta.py"] == {"imports": ["src.beta"]}
    alpha = analyzer.discover("tests/test_alpha.py")
    beta = analyzer.discover("tests/test_beta.py")
    assert alpha["source_files"] == ["src/alpha.py"]
    assert beta["source_files"] == ["src/beta.py"]


def test_overlapping_scan_batches_reuse_nodes_with_full_lexical_context(tmp_path, monkeypatch):
    write(tmp_path, "src/used.py", "VALUE = 1")
    write(tmp_path, "src/other.py", "VALUE = 2")
    write(tmp_path, "docs/example.md", "example")
    path = "tests/test_sample.py"
    write(tmp_path, path, """
        import subprocess
        import sys
        SCRIPT = "from src import used"
        def first(request):
            subprocess.run([sys.executable, "-c", SCRIPT])
            request.getfixturevalue("needed")
            resource = "docs/example.md"
        def second(SCRIPT):
            subprocess.run([sys.executable, "-c", SCRIPT])
        class Nested:
            SCRIPT = "from src import other"
            def third(self):
                subprocess.run([sys.executable, "-c", SCRIPT])
    """)
    baseline = DependencyAnalyzer(tmp_path)
    expected = baseline._scan_nodes(path, baseline.tree(path).body)
    analyzer = DependencyAnalyzer(tmp_path)
    tree = analyzer.tree(path)
    calls = []
    original = analyzer._scan_nodes

    def scan(current_path, nodes, aliases_from=None):
        calls.extend(id(node) for node in nodes)
        return original(current_path, nodes, aliases_from)

    monkeypatch.setattr(analyzer, "_scan_nodes", scan)
    assert analyzer._scan(path, tree.body) == expected
    assert analyzer._scan(path, list(reversed(tree.body))) == expected
    analyzer._scan(path, tree.body[-2:])
    assert all(calls.count(id(node)) == 1 for node in tree.body)
    assert {"src/used.py", "src/other.py"} <= expected[0]
    assert expected[2] == {"needed"}
    assert expected[3]  # The parameter shadows SCRIPT in second().
    assert "docs/example.md" in expected[4] and expected[5]
    assert len(analyzer.scan_contexts) == 3  # Source and two literal subprocess trees.
    assert analyzer._scan_context(path, tree)[0] is analyzer.parents[path]


def test_shared_generator_trees_initialize_parents_and_refresh_per_graph(tmp_path, monkeypatch):
    from scripts.bazel import generate_targets as targets

    write(tmp_path, "src/used.py", "VALUE = 1")
    write(tmp_path, "src/other.py", "VALUE = 2")
    path = "tests/test_sample.py"
    write(tmp_path, path, "from src.used import VALUE")
    snapshots = []
    original = targets.dependency_analyzer

    def analyzer(root, trees=None):
        instance = original(root, trees)
        assert trees and instance.trees is trees
        shared = trees["src/used.py"]
        assert instance.tree("src/used.py") is shared
        assert "src/used.py" in instance.parents
        snapshots.append(trees)
        return instance

    monkeypatch.setattr(targets, "dependency_analyzer", analyzer)
    _, before = targets.graph(tmp_path, {path: {}}, fine_grained={path})
    write(tmp_path, "src/used.py", "from src.other import VALUE")
    _, after = targets.graph(tmp_path, {path: {}}, fine_grained={path})
    assert "src/other.py" not in before[path]["source_files"]
    assert "src/other.py" in after[path]["source_files"]
    assert snapshots[0] is not snapshots[1]
    assert snapshots[0]["src/used.py"] is not snapshots[1]["src/used.py"]


def test_node_scan_cache_includes_lazy_package_recursion_guard(tmp_path):
    write(tmp_path, "src/pkg/__init__.py", """
        def __getattr__(name):
            from src import lazy_input
            return lazy_input
    """)
    write(tmp_path, "src/lazy_input.py", "VALUE = 1")
    path = "tests/test_sample.py"
    write(tmp_path, path, "from src.pkg import dynamic")
    analyzer = DependencyAnalyzer(tmp_path)
    nodes = analyzer.tree(path).body
    analyzer.lazy_scanning.add("src/pkg/__init__.py")
    guarded = analyzer._scan(path, nodes)
    assert "src/lazy_input.py" not in guarded[0]
    analyzer.lazy_scanning.clear()
    unguarded = analyzer._scan(path, nodes)
    assert "src/lazy_input.py" in unguarded[0]
    analyzer.lazy_scanning.add("src/pkg/__init__.py")
    assert analyzer._scan(path, nodes) == guarded
