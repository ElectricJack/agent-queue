"""Per-test Bazel dependency discovery preserves runtime inputs without global fanout."""
import ast
from pathlib import Path
from textwrap import dedent

from scripts.bazel.test_dependencies import DependencyAnalyzer, discover


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


def test_repeated_directory_literals_expand_once_per_analyzer(tmp_path, monkeypatch):
    write(tmp_path, "src/templates/example.txt", "template")
    for name in ("first", "second"):
        write(tmp_path, f"tests/test_{name}.py", '''
            from pathlib import Path
            def test_sample():
                paths = list(Path("src/templates").glob("*"))
                assert paths
        ''')
    expansions = []
    original = Path.rglob

    def counted(path, pattern, *args, **kwargs):
        if path == tmp_path / "src/templates" and pattern == "*":
            expansions.append(path)
        return original(path, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "rglob", counted)
    analyzer = DependencyAnalyzer(tmp_path)
    for name in ("first", "second"):
        result = analyzer.discover(f"tests/test_{name}.py")
        assert result["data_files"] == ["src/templates/example.txt"]
    assert len(expansions) == 1


def test_nested_path_expressions_walk_each_subtree_once(tmp_path, monkeypatch):
    write(tmp_path, "docs/guide.md", "guide")
    expression = 'Path("docs/guide.md")'
    for _ in range(40):
        expression = f"wrap({expression})"
    source = f"from pathlib import Path\ndef test_sample():\n    path = {expression}\n"
    write(tmp_path, "tests/test_sample.py", source)
    original = ast.walk
    node_count = len(list(original(ast.parse(source))))
    visits = 0

    def counted(node):
        nonlocal visits
        for child in original(node):
            visits += 1
            yield child

    monkeypatch.setattr(ast, "walk", counted)
    result = discover(tmp_path, "tests/test_sample.py")
    assert result["docs"]
    assert result["data_files"] == ["docs/guide.md"]
    # Alias indexing, fixture discovery and literal collection are linear walks.
    assert visits <= 4 * node_count


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
    write(tmp_path, "tests/test_sample.py", "import src.entry")
    assert discover(tmp_path, "tests/test_sample.py")["source_files"] == [
        "src/entry.py", "src/runtime.py",
    ]


def test_directory_data_excludes_build_metadata(tmp_path):
    for checkout in ("repo", ".aq/worktrees/slot-9", ".venv/repo"):
        root = tmp_path / checkout
        write(root, "src/templates/example.md", "template")
        for metadata in ("BUILD", "BUILD.bazel", ".aq/state.json", ".venv/cache.txt",
                         ".git/config", "__pycache__/cache.pyc", "node_modules/cache.js"):
            write(root, f"src/templates/{metadata}", "excluded")
        write(root, "tests/test_sample.py", """
            from pathlib import Path
            def test_sample():
                assert list(Path("src/templates").glob("*.md"))
        """)
        result = discover(root, "tests/test_sample.py")
        assert result["data_files"] == ["src/templates/example.md"], checkout


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
