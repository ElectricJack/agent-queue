"""Python subprocess imports must remain present in narrowed Bazel runfiles."""

from pathlib import Path
from textwrap import dedent

import pytest

from scripts.bazel.test_dependencies import discover


def write(root: Path, path: str, source: str = "") -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(dedent(source), encoding="utf-8")


def test_cached_path_context_keeps_reader_relative_paths_separate(tmp_path):
    import ast

    from scripts.bazel.dependency_paths import repository_data

    source = "from pathlib import Path\n(Path(__file__).parent / 'input.json').read_text()\n"
    tree = ast.parse(source)
    cache = {}
    for directory in ("one", "two"):
        reader = f"tests/{directory}/test_reader.py"
        resource = f"tests/{directory}/input.json"
        write(tmp_path, reader, source)
        write(tmp_path, resource, "{}")
        assert repository_data(
            tmp_path, reader, tree.body, context=tree, context_cache=cache,
        ) == {resource}
    assert len(cache) == 1


@pytest.mark.parametrize("command", [
    'subprocess.run([sys.executable, "-I", "-c", "import src.child"])',
    'subprocess.check_output(args=("python3.12", "-c", "import src.child"))',
    'subprocess.Popen(["C:/Python/python.exe", "-c", "import src.child"])',
])
def test_literal_python_subprocess_imports_follow_transitive_sources(tmp_path, command):
    write(tmp_path, "src/__init__.py")
    write(tmp_path, "src/child.py", "from src import helper")
    write(tmp_path, "src/helper.py")
    write(tmp_path, "src/unrelated.py")
    write(tmp_path, "tests/test_child.py", f"import subprocess, sys\n{command}")

    result = discover(tmp_path, "tests/test_child.py")

    assert result["source_files"] == ["src/__init__.py", "src/child.py", "src/helper.py"]
    assert not result["conservative"], result["conservative_reasons"]


def test_subprocess_and_snippet_import_aliases_are_resolved(tmp_path):
    write(tmp_path, "src/child.py")
    write(tmp_path, "src/unrelated.py")
    write(tmp_path, "tests/test_child.py", '''
        from subprocess import run as execute
        import sys as interpreter
        from textwrap import dedent as clean

        execute([interpreter.executable, "-c", clean("""
            from importlib import import_module as load
            load("src.child")
        """)])
    ''')

    result = discover(tmp_path, "tests/test_child.py")

    assert result["source_files"] == ["src/child.py"]
    assert not result["conservative"], result["conservative_reasons"]


@pytest.mark.parametrize("snippet", ["source", 'f"import {module}"', '"invalid python !!!"'])
def test_unresolved_python_subprocess_widens_with_visible_reason(tmp_path, snippet):
    write(tmp_path, "src/child.py")
    write(tmp_path, "src/unrelated.py")
    write(tmp_path, "tests/child_support.py")
    write(tmp_path, "tests/test_child.py", f'''
        import subprocess, sys
        from textwrap import dedent

        def execute(source, module):
            subprocess.run([sys.executable, "-c", dedent({snippet})])
    ''')

    result = discover(tmp_path, "tests/test_child.py")

    assert result["source_files"] == ["src/child.py", "src/unrelated.py"]
    assert "tests/child_support.py" in result["test_sources"]
    assert result["conservative_reasons"] == [
        "tests/test_child.py: unresolved dynamic import in subprocess Python execution",
    ]


def test_non_python_subprocess_does_not_invent_python_dependencies(tmp_path):
    write(tmp_path, "src/unrelated.py")
    write(tmp_path, "tests/test_child.py", '''
        import subprocess
        subprocess.run(["bash", "-c", "import src.unrelated"])
    ''')

    result = discover(tmp_path, "tests/test_child.py")

    assert result["source_files"] == []
    assert not result["conservative"]


@pytest.mark.parametrize("source", [
    'from alembic import command\ncommand.upgrade(config, "head")',
    'import alembic.command as migrations\nmigrations.downgrade(config, "base")',
    'from alembic.command import upgrade as migrate\nmigrate(config, "head")',
])
def test_alembic_execution_follows_environment_and_revision_imports(tmp_path, source):
    write(tmp_path, "src/environment.py", "from src import environment_helper")
    write(tmp_path, "src/environment_helper.py")
    write(tmp_path, "src/revision_helper.py")
    write(tmp_path, "src/unrelated.py")
    write(tmp_path, "migrations/env.py", "import src.environment")
    write(tmp_path, "migrations/versions/first.py", "import src.revision_helper")
    write(tmp_path, "tests/test_migrate.py", source)

    result = discover(tmp_path, "tests/test_migrate.py")

    assert result["source_files"] == [
        "src/environment.py", "src/environment_helper.py", "src/revision_helper.py",
    ]
    assert result["other_sources"] == ["migrations/env.py", "migrations/versions/first.py"]
    assert not result["conservative"], result["conservative_reasons"]


def test_overridden_migration_fixture_does_not_execute_its_environment(tmp_path):
    write(tmp_path, "src/installation.py")
    write(tmp_path, "src/environment.py")
    write(tmp_path, "migrations/env.py", "import src.environment")
    write(tmp_path, "tests/conftest.py", '''
        import pytest
        @pytest.fixture(autouse=True)
        def database():
            from alembic import command
            command.upgrade(config, "head")
    ''')
    write(tmp_path, "tests/test_install.py", '''
        import pytest
        import src.installation
        @pytest.fixture(autouse=True)
        def database():
            pass
        def test_install(): pass
    ''')

    result = discover(tmp_path, "tests/test_install.py")

    assert result["source_files"] == ["src/installation.py"]
    assert result["other_sources"] == []
    assert not result["conservative"]


def test_imported_engine_declares_migrations_despite_pure_fixture_override(tmp_path):
    write(tmp_path, "src/installation.py")
    write(tmp_path, "src/environment.py")
    write(tmp_path, "src/unrelated.py")
    write(tmp_path, "src/engine.py", '''
        def initialize():
            from alembic import command
            command.upgrade(config, "head")
    ''')
    write(tmp_path, "migrations/env.py", "import src.environment")
    write(tmp_path, "tests/conftest.py", '''
        import pytest
        import src.engine
        @pytest.fixture(autouse=True)
        def database():
            src.engine.initialize()
    ''')
    write(tmp_path, "tests/test_install.py", '''
        import pytest
        import src.installation
        @pytest.fixture(autouse=True)
        def database():
            pass
        def test_install(): pass
    ''')

    result = discover(tmp_path, "tests/test_install.py")

    # Production functions are scanned in full. Declaring their potential
    # runtime imports is safe even if this module never calls initialize().
    assert result["source_files"] == [
        "src/engine.py", "src/environment.py", "src/installation.py",
    ]
    assert result["other_sources"] == ["migrations/env.py"]
    assert not result["conservative"]


def test_composed_repository_paths_preserve_migration_source_readers(tmp_path):
    import ast

    from scripts.bazel.dependency_paths import repository_data

    for path in (
        "src/database/tables.py", "src/database/unrelated.py",
        "migrations/versions/first.py", "migrations/versions/second.py",
    ):
        write(tmp_path, path)
    nodes = ast.parse(dedent('''
        from pathlib import Path
        ROOT = Path(__file__).resolve().parent.parent
        database = ROOT / "src" / "database"
        SOURCES = sorted((ROOT / "migrations" / "versions").glob("*.py")) + [
            database / "tables.py"
        ]
    ''')).body
    assert repository_data(tmp_path, "tests/test_scan.py", nodes) == {
        "src/database/tables.py", "migrations/versions/first.py",
        "migrations/versions/second.py",
    }


def test_repository_path_aliases_and_explicit_recursive_globs(tmp_path):
    import ast

    from scripts.bazel.dependency_paths import repository_data

    for path in ("src/templates/top.txt", "src/templates/nested/leaf.txt",
                 "src/templates/BUILD.bazel", "src/templates/__pycache__/cache.py"):
        write(tmp_path, path)
    nodes = ast.parse(dedent('''
        from pathlib import Path as P
        ROOT = P(__file__).absolute().parents[1]
        templates = ROOT / "src" / "templates"
        resources = templates.rglob("*.txt")
    ''')).body
    assert repository_data(tmp_path, "tests/test_sample.py", nodes) == {
        "src/templates/top.txt", "src/templates/nested/leaf.txt",
    }


def test_repository_paths_reject_dynamic_bindings_and_escape(tmp_path):
    import ast

    from scripts.bazel.dependency_paths import repository_data

    write(tmp_path, "src/known.py")
    outside = tmp_path.parent / (tmp_path.name + "-outside.txt")
    outside.write_text("outside", encoding="utf-8")
    nodes = ast.parse(dedent(f'''
        from pathlib import Path
        ROOT = Path(__file__).resolve().parents[1]
        dynamic = ROOT / component / "known.py"
        escaped = Path({str(outside)!r})
        unknown = tmp_path / "src" / "known.py"
    ''')).body
    assert repository_data(tmp_path, "tests/test_sample.py", nodes) == set()


def test_repository_path_discovery_tracks_schema_key_metadata_inputs():
    import ast

    from scripts.bazel.dependency_paths import repository_data

    root = Path(__file__).resolve().parents[1]
    path = "src/database/schema_key.py"
    result = repository_data(root, path, ast.parse((root / path).read_text()).body)
    assert {
        "alembic.ini", "src/database/tables.py", "src/records/schema.py",
        "src/knowledge/protection_schema.py", "src/database/hierarchy_migration.py",
        "migrations/env.py", "migrations/integration_guards.py",
    } <= result
    assert {
        p.relative_to(root).as_posix()
        for p in (root / "migrations/versions").glob("*.py")
    } <= result
    assert "src/database/engine.py" not in result


@pytest.mark.parametrize("assignment", [
    'code = "import src.child"',
    'code = dedent("import src.child")',
    'original = "import src.child"\ncode = original',
])
def test_named_literal_python_subprocess_snippet(tmp_path, assignment):
    write(tmp_path, "src/child.py")
    write(tmp_path, "src/unrelated.py")
    write(tmp_path, "tests/test_child.py", (
        "import subprocess, sys\nfrom textwrap import dedent\n" + assignment
        + '\nsubprocess.run([sys.executable, "-c", code])\n'
    ))

    result = discover(tmp_path, "tests/test_child.py")

    assert result["source_files"] == ["src/child.py"]
    assert not result["conservative"], result["conservative_reasons"]


@pytest.mark.parametrize("source", [
    '''
        code = "import src.child"
        code = source
        subprocess.run([sys.executable, "-c", code])
    ''',
    '''
        code = "import src.child"
        code += source
        subprocess.run([sys.executable, "-c", code])
    ''',
    '''
        code = "import src.child"
        def launch(code):
            subprocess.run([sys.executable, "-c", code])
    ''',
    '''
        def unrelated():
            code = "import src.child"
        def launch(source):
            subprocess.run([sys.executable, "-c", code])
    ''',
])
def test_named_subprocess_conflicting_mutated_or_shadowed_code_is_unknown(tmp_path, source):
    write(tmp_path, "src/child.py")
    write(tmp_path, "src/unrelated.py")
    write(tmp_path, "tests/test_child.py", "import subprocess, sys\n" + dedent(source))

    result = discover(tmp_path, "tests/test_child.py")

    assert result["source_files"] == ["src/child.py", "src/unrelated.py"]
    assert result["conservative_reasons"] == [
        "tests/test_child.py: unresolved dynamic import in subprocess Python execution",
    ]


def test_bare_repository_directory_assignment_does_not_expand(tmp_path):
    import ast

    from scripts.bazel.dependency_paths import repository_data

    write(tmp_path, "src/kept.py")
    write(tmp_path, "src/nested/unrelated.py")
    nodes = ast.parse('''
from pathlib import Path
ROOT = Path(__file__).parents[1]
directory = ROOT / "src"
''').body

    assert repository_data(tmp_path, "tests/test_reader.py", nodes) == set()


@pytest.mark.parametrize("read_self", [False, True])
def test_resolving_repository_root_does_not_read_current_file(tmp_path, read_self):
    import ast

    from scripts.bazel.dependency_paths import repository_data

    path = "tests/test_reader.py"
    source = '''
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
'''
    if read_self:
        source += "contents = Path(__file__).read_text()\n"
    write(tmp_path, path, source)

    result = repository_data(tmp_path, path, ast.parse(source).body)

    assert result == ({path} if read_self else set())


@pytest.mark.parametrize("reader", [
    'directory.iterdir()',
    'directory.walk()',
    'directory.is_dir()',
    'Path("src/").is_dir()',
    'os.listdir(directory)',
    'os.walk(top=directory)',
    'listing(directory)',
    'scan(str(directory))',
    'assigned_reader(directory)',
])
def test_explicit_repository_directory_iteration_retains_tree(tmp_path, reader):
    import ast

    from scripts.bazel.dependency_paths import repository_data

    for name in ("src/top.py", "src/nested/leaf.py", "src/BUILD.bazel",
                 "src/__pycache__/cache.py", "src/node_modules/ignored.js",
                 "src/build.tsbuildinfo"):
        write(tmp_path, name)
    write(tmp_path, "other/unrelated.py")
    nodes = ast.parse('''
import os
from os import listdir as listing, walk as scan
from pathlib import Path
ROOT = Path(__file__).parents[1]
directory = ROOT / "src"
assigned_reader = scan
''' + reader).body

    assert repository_data(tmp_path, "tests/test_reader.py", nodes) == {
        "src/top.py", "src/nested/leaf.py",
    }


def test_directory_reads_exclude_dashboard_build_outputs_in_aq_worktree(tmp_path):
    import ast

    from scripts.bazel.dependency_paths import repository_data

    root = tmp_path / ".aq" / "worktree"
    for name in ("dashboard/src/kept.ts", "dashboard/dist/bundle.js",
                 "dashboard/src/build.tsbuildinfo", "dashboard/node_modules/lib.js"):
        write(root, name)
    nodes = ast.parse('''
from pathlib import Path
ROOT = Path(__file__).parents[1]
files = (ROOT / "dashboard").rglob("*")
''').body

    assert repository_data(root, "tests/test_reader.py", nodes) == {
        "dashboard/src/kept.ts",
    }


def test_selected_reader_uses_global_bindings_without_reading_inactive_body(tmp_path):
    import ast

    from scripts.bazel.dependency_paths import repository_data

    write(tmp_path, "dashboard/src/kept.ts")
    write(tmp_path, "other/inactive.txt")
    tree = ast.parse('''
from pathlib import Path as P
ROOT = P(__file__).parents[1]
DIRECTORY = ROOT / "dashboard" / "src"
def active():
    return list(DIRECTORY.glob("*.ts"))
def inactive():
    return (ROOT / "other" / "inactive.txt").read_text()
''')
    active = next(node for node in tree.body if getattr(node, "name", None) == "active")

    assert repository_data(tmp_path, "tests/test_reader.py", active.body, context=tree) == {
        "dashboard/src/kept.ts",
    }


def test_selected_reader_parameter_does_not_reuse_unrelated_global_path(tmp_path):
    import ast

    from scripts.bazel.dependency_paths import repository_data

    write(tmp_path, "src/unrelated.py")
    tree = ast.parse('''
from pathlib import Path
ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "src" / "unrelated.py"
def active(SOURCE):
    return SOURCE.read_text()
''')
    active = tree.body[-1]

    assert repository_data(tmp_path, "tests/test_reader.py", active.body, context=tree) == set()


def test_repository_reader_local_bindings_are_scoped_to_their_functions(tmp_path):
    import ast

    from scripts.bazel.dependency_paths import repository_data

    write(tmp_path, "src/first.py")
    write(tmp_path, "src/second.py")
    tree = ast.parse('''
from pathlib import Path
ROOT = Path(__file__).parents[1]
def first():
    directory = ROOT / "src"
    return (directory / "first.py").read_text()
def second():
    directory = ROOT / "unknown"
    return (ROOT / "src" / "second.py").read_text()
''')

    assert repository_data(tmp_path, "tests/test_reader.py", tree.body) == {
        "src/first.py", "src/second.py",
    }


def test_imported_selected_helper_follows_local_calls_without_inactive_migration(tmp_path):
    for path in ("src/selected.py", "src/transitive.py", "src/environment.py"):
        write(tmp_path, path)
    write(tmp_path, "src/engine.py", '''
        from alembic import command
        def migrate(): command.upgrade(config, "head")
    ''')
    write(tmp_path, "migrations/env.py", "import src.environment")
    write(tmp_path, "tests/helpers.py", '''
        def inactive():
            from src.engine import migrate
            migrate()
        def local():
            import src.transitive
        def selected():
            local()
            import src.selected
    ''')
    write(tmp_path, "tests/test_helper.py", '''
        from tests.helpers import selected as run_selected
        def test_selected(): run_selected()
    ''')

    result = discover(tmp_path, "tests/test_helper.py")

    assert result["source_files"] == ["src/selected.py", "src/transitive.py"]
    assert result["other_sources"] == []
    assert not result["conservative"], result["conservative_reasons"]


def test_imported_helper_module_alias_selects_qualified_function(tmp_path):
    write(tmp_path, "src/selected.py")
    write(tmp_path, "src/unrelated.py")
    write(tmp_path, "tests/helpers.py", '''
        def selected():
            import src.selected
        def unused():
            import src.unrelated
    ''')
    write(tmp_path, "tests/test_helper.py", '''
        import tests.helpers as support
        def test_selected(): support.selected()
    ''')

    result = discover(tmp_path, "tests/test_helper.py")

    assert result["source_files"] == ["src/selected.py"]
    assert not result["conservative"], result["conservative_reasons"]


def test_inactive_imported_helper_defaults_and_decorators_still_execute(tmp_path):
    for name in ("default", "decorator", "inactive"):
        write(tmp_path, f"src/{name}.py")
    write(tmp_path, "tests/helpers.py", '''
        def decorate(function):
            import src.decorator
            return function
        def default_value():
            import src.default
            return None
        @decorate
        def inactive(value=default_value()):
            import src.inactive
        def selected():
            pass
    ''')
    write(tmp_path, "tests/test_helper.py", '''
        from tests.helpers import selected
        def test_selected(): selected()
    ''')

    result = discover(tmp_path, "tests/test_helper.py")

    assert result["source_files"] == ["src/decorator.py", "src/default.py"]
    assert not result["conservative"], result["conservative_reasons"]


@pytest.mark.parametrize("lookup", [
    'globals()[name]()',
    'getattr(sys.modules[__name__], name)()',
    'exec(name)',
])
def test_dynamic_imported_helper_dispatch_keeps_conservative_inputs(tmp_path, lookup):
    write(tmp_path, "src/selected.py")
    write(tmp_path, "src/unrelated.py")
    write(tmp_path, "tests/helpers.py", '''
import sys
def selected():
    import src.selected
def unused():
    import src.unrelated
def choose(name):
    ''' + lookup + '\n')
    write(tmp_path, "tests/test_helper.py", '''
        from tests.helpers import choose
        def test_choose(): choose("selected")
    ''')

    result = discover(tmp_path, "tests/test_helper.py")

    assert result["source_files"] == ["src/selected.py", "src/unrelated.py"]
    assert result["conservative"], result["conservative_reasons"]
    assert any("dynamic" in reason for reason in result["conservative_reasons"])


def test_imported_helper_without_use_retains_only_import_time_dependencies(tmp_path):
    write(tmp_path, "src/startup.py")
    write(tmp_path, "src/unused.py")
    write(tmp_path, "tests/helpers.py", '''
        import src.startup
        def inactive():
            import src.unused
    ''')
    write(tmp_path, "tests/test_helper.py", '''
        from tests.helpers import inactive as unused_helper
        def test_independent(): pass
    ''')

    result = discover(tmp_path, "tests/test_helper.py")

    assert result["source_files"] == ["src/startup.py"]
    assert not result["conservative"], result["conservative_reasons"]


@pytest.mark.parametrize("entry", [
    'from tests.facade import exposed as invoke\ndef test_selected(): invoke()',
    'from tests import facade as api\ndef test_selected(): api.exposed()',
])
def test_active_import_alias_follows_transitive_renamed_helper_reexports(tmp_path, entry):
    for name in ("selected", "transitive", "unused"):
        write(tmp_path, f"src/{name}.py")
    write(tmp_path, "tests/helpers.py", '''
        def local():
            import src.transitive
        def original():
            local()
            import src.selected
        def inactive():
            import src.unused
    ''')
    write(tmp_path, "tests/reexport.py", "from tests.helpers import original as public")
    write(tmp_path, "tests/facade.py", "from .reexport import public as exposed")
    write(tmp_path, "tests/test_helper.py", entry)

    result = discover(tmp_path, "tests/test_helper.py")

    assert result["source_files"] == ["src/selected.py", "src/transitive.py"]
    assert not result["conservative"], result["conservative_reasons"]


@pytest.mark.parametrize("module_alias", [False, True])
def test_helper_alias_collisions_across_functions_retain_all_active_targets(
    tmp_path, module_alias,
):
    for name in ("first", "second", "unused"):
        write(tmp_path, f"src/{name}.py")
    write(tmp_path, "tests/helpers.py", '''
        def first():
            import src.first
        def second():
            import src.second
        def inactive():
            import src.unused
    ''')
    write(tmp_path, "tests/facade_first.py", "from tests.helpers import first as exposed")
    write(tmp_path, "tests/facade_second.py", "from tests.helpers import second as exposed")
    if module_alias:
        entry = '''
            def test_first():
                from tests import facade_first as support
                support.exposed()
            def test_second():
                from tests import facade_second as support
                support.exposed()
        '''
    else:
        entry = '''
            def test_first():
                from tests.helpers import first as invoke
                invoke()
            def test_second():
                from tests.helpers import second as invoke
                invoke()
        '''
    write(tmp_path, "tests/test_helper.py", entry)

    result = discover(tmp_path, "tests/test_helper.py")

    assert result["source_files"] == ["src/first.py", "src/second.py"]
    assert not result["conservative"], result["conservative_reasons"]


@pytest.mark.parametrize("instantiate", [False, True])
def test_imported_class_keeps_initialization_inputs_and_selects_methods_only_when_used(
    tmp_path, instantiate,
):
    for name in ("class_startup", "decorator", "default", "method", "selected"):
        write(tmp_path, f"src/{name}.py")
    write(tmp_path, "tests/helpers.py", '''
        def decorate(cls):
            import src.decorator
            return cls
        def initialize():
            import src.default
            return None
        @decorate
        class Holder:
            import src.class_startup
            def method(self, value=initialize()):
                import src.method
        def selected():
            import src.selected
    ''')
    entry = "from tests.helpers import selected\ndef test_selected(): selected()\n"
    if instantiate:
        entry += "from tests.helpers import Holder\ndef test_holder(): Holder()\n"
    write(tmp_path, "tests/test_helper.py", entry)

    result = discover(tmp_path, "tests/test_helper.py")

    expected = ["src/class_startup.py", "src/decorator.py", "src/default.py", "src/selected.py"]
    if instantiate:
        expected.append("src/method.py")
    assert result["source_files"] == sorted(expected)
    assert not result["conservative"], result["conservative_reasons"]
