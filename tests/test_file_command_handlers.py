"""Unit tests for filesystem command handlers.

Tests: edit_file, glob_files, grep, read_file (with offset), list_directory,
write_file.  All handlers are tested through handler.execute() with real
temp-directory files and a real SQLite database.  Path validation (sandbox
security) is tested separately at the end.
"""

import os
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Project, RepoConfig, RepoSourceType, Workspace
from src.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    """Create a real in-memory database for tests."""
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
def mock_git():
    from src.git.manager import GitManager

    git = MagicMock(spec=GitManager)
    return git


@pytest.fixture
async def handler(db, config, mock_git):
    """Create a CommandHandler with mocked orchestrator and internal plugins."""
    from src.event_bus import EventBus
    from src.plugins.registry import PluginRegistry
    from src.plugins.services import build_internal_services

    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = mock_git

    services = build_internal_services(db=db, git=mock_git, config=config)
    registry = PluginRegistry(db=db, bus=EventBus(), config=config)
    registry._internal_services = services
    await registry.load_internal_plugins()
    orchestrator.plugin_registry = registry

    handler = CommandHandler(orchestrator, config)
    registry.set_active_project_id_getter(lambda: handler._active_project_id)
    return handler


@pytest.fixture
def workspace_dir(tmp_path):
    """Create a workspace directory with sample files."""
    ws = tmp_path / "workspaces" / "test-proj"
    ws.mkdir(parents=True)
    return ws


@pytest.fixture
async def project_with_workspace(db, workspace_dir):
    """Create a test project with a linked workspace.

    Returns (project_id, workspace_path).
    """
    project_id = "test-proj"
    await db.create_project(Project(id=project_id, name="Test Project"))
    await db.create_repo(
        RepoConfig(
            id="test-repo",
            project_id=project_id,
            source_type=RepoSourceType.LINK,
            source_path=str(workspace_dir),
            default_branch="main",
        )
    )
    await db.create_workspace(
        Workspace(
            id="test-ws",
            project_id=project_id,
            workspace_path=str(workspace_dir),
            source_type=RepoSourceType.LINK,
            name="default",
        )
    )
    return project_id, str(workspace_dir)


# ---------------------------------------------------------------------------
# edit_file tests
# ---------------------------------------------------------------------------


class TestEditFile:
    async def test_single_replacement(self, handler, workspace_dir):
        """Happy path: replace a unique string once."""
        target = workspace_dir / "hello.py"
        target.write_text("def foo():\n    return 1\n")

        result = await handler.execute(
            "edit_file",
            {
                "path": str(target),
                "old_string": "def foo",
                "new_string": "def bar",
            },
        )

        assert "error" not in result
        assert result["replacements"] == 1
        assert "def bar" in target.read_text()

    async def test_ambiguous_match(self, handler, workspace_dir):
        """Error when old_string matches multiple times and replace_all=False."""
        target = workspace_dir / "dup.py"
        target.write_text("x = 1\nx = 2\nx = 3\n")

        result = await handler.execute(
            "edit_file",
            {
                "path": str(target),
                "old_string": "x = ",
                "new_string": "y = ",
            },
        )

        assert "error" in result
        assert "3 times" in result["error"]
        # File should be unchanged
        assert target.read_text() == "x = 1\nx = 2\nx = 3\n"

    async def test_replace_all(self, handler, workspace_dir):
        """replace_all=True should replace every occurrence."""
        target = workspace_dir / "multi.py"
        target.write_text("x = 1\nx = 2\nx = 3\n")

        result = await handler.execute(
            "edit_file",
            {
                "path": str(target),
                "old_string": "x = ",
                "new_string": "y = ",
                "replace_all": True,
            },
        )

        assert "error" not in result
        assert result["replacements"] == 3
        assert target.read_text() == "y = 1\ny = 2\ny = 3\n"

    async def test_no_match(self, handler, workspace_dir):
        """Error when old_string is not found in the file."""
        target = workspace_dir / "no_match.py"
        target.write_text("hello world\n")

        result = await handler.execute(
            "edit_file",
            {
                "path": str(target),
                "old_string": "goodbye",
                "new_string": "hi",
            },
        )

        assert "error" in result
        assert "not found" in result["error"]

    async def test_file_not_found(self, handler, workspace_dir):
        """Error when path points to a nonexistent file."""
        result = await handler.execute(
            "edit_file",
            {
                "path": str(workspace_dir / "nonexistent.py"),
                "old_string": "x",
                "new_string": "y",
            },
        )

        assert "error" in result
        assert "not found" in result["error"].lower() or "File not found" in result["error"]


# ---------------------------------------------------------------------------
# glob_files tests
# ---------------------------------------------------------------------------


class TestGlobFiles:
    async def test_basic(self, handler, workspace_dir):
        """Find Python files in a directory."""
        (workspace_dir / "a.py").write_text("# a")
        (workspace_dir / "b.py").write_text("# b")
        (workspace_dir / "c.txt").write_text("c")

        result = await handler.execute(
            "glob_files",
            {
                "pattern": "*.py",
                "path": str(workspace_dir),
            },
        )

        assert "error" not in result
        assert result["count"] == 2
        names = set(result["matches"])
        assert "a.py" in names
        assert "b.py" in names
        assert "c.txt" not in names

    async def test_recursive(self, handler, workspace_dir):
        """**/*.py pattern finds files in subdirectories."""
        sub = workspace_dir / "pkg"
        sub.mkdir()
        (workspace_dir / "top.py").write_text("# top")
        (sub / "nested.py").write_text("# nested")

        result = await handler.execute(
            "glob_files",
            {
                "pattern": "**/*.py",
                "path": str(workspace_dir),
            },
        )

        assert "error" not in result
        assert result["count"] == 2
        matches = result["matches"]
        assert any("nested.py" in m for m in matches)
        assert any("top.py" in m for m in matches)

    async def test_no_matches(self, handler, workspace_dir):
        """Returns empty list when no files match."""
        result = await handler.execute(
            "glob_files",
            {
                "pattern": "*.rs",
                "path": str(workspace_dir),
            },
        )

        assert "error" not in result
        assert result["count"] == 0
        assert result["matches"] == []

    async def test_directory_not_found(self, handler, workspace_dir):
        """Error when the search path doesn't exist."""
        result = await handler.execute(
            "glob_files",
            {
                "pattern": "*.py",
                "path": str(workspace_dir / "nonexistent"),
            },
        )

        assert "error" in result


# ---------------------------------------------------------------------------
# grep tests
# ---------------------------------------------------------------------------


class TestGrep:
    async def test_basic(self, handler, workspace_dir):
        """Content search finds matching lines."""
        (workspace_dir / "source.py").write_text("import os\nimport sys\nprint('hello')\n")

        result = await handler.execute(
            "grep",
            {
                "pattern": "import",
                "path": str(workspace_dir),
            },
        )

        assert "error" not in result
        assert "import os" in result["results"]
        assert "import sys" in result["results"]

    async def test_context_lines(self, handler, workspace_dir):
        """Context lines are included in output."""
        (workspace_dir / "ctx.py").write_text("line1\nline2\nTARGET\nline4\nline5\n")

        result = await handler.execute(
            "grep",
            {
                "pattern": "TARGET",
                "path": str(workspace_dir),
                "context": 1,
            },
        )

        assert "error" not in result
        assert "line2" in result["results"]
        assert "TARGET" in result["results"]
        assert "line4" in result["results"]

    async def test_case_insensitive(self, handler, workspace_dir):
        """case_insensitive=True matches regardless of case."""
        (workspace_dir / "ci.py").write_text("Hello World\nhello world\n")

        result = await handler.execute(
            "grep",
            {
                "pattern": "HELLO",
                "path": str(workspace_dir),
                "case_insensitive": True,
            },
        )

        assert "error" not in result
        assert "Hello" in result["results"]
        assert "hello" in result["results"]

    async def test_files_only(self, handler, workspace_dir):
        """output_mode=files_with_matches returns only paths."""
        (workspace_dir / "match.py").write_text("needle here\n")
        (workspace_dir / "nope.py").write_text("nothing\n")

        result = await handler.execute(
            "grep",
            {
                "pattern": "needle",
                "path": str(workspace_dir),
                "output_mode": "files_with_matches",
            },
        )

        assert "error" not in result
        assert result["mode"] == "files_with_matches"
        assert "match.py" in result["results"]
        # files_with_matches mode should NOT show matched line content
        assert "needle here" not in result["results"] or result["results"].strip().endswith(
            "match.py"
        )

    async def test_no_matches(self, handler, workspace_dir):
        """Returns '(no matches)' when nothing matches."""
        (workspace_dir / "empty.py").write_text("nothing relevant\n")

        result = await handler.execute(
            "grep",
            {
                "pattern": "zzzzz_nonexistent_pattern",
                "path": str(workspace_dir),
            },
        )

        assert "error" not in result
        assert result["results"] == "(no matches)"

    async def test_path_not_found(self, handler, workspace_dir):
        """Error when search path doesn't exist."""
        result = await handler.execute(
            "grep",
            {
                "pattern": "test",
                "path": str(workspace_dir / "nonexistent"),
            },
        )

        assert "error" in result


# ---------------------------------------------------------------------------
# read_file tests
# ---------------------------------------------------------------------------


class TestReadFile:
    async def test_basic_read(self, handler, workspace_dir):
        """Read a file and get its content."""
        target = workspace_dir / "readme.txt"
        target.write_text("line 1\nline 2\nline 3\n")

        result = await handler.execute(
            "read_file",
            {
                "path": str(target),
            },
        )

        assert "error" not in result
        assert "line 1" in result["content"]
        assert "line 3" in result["content"]

    async def test_with_offset(self, handler, workspace_dir):
        """offset skips lines — reading from line 3 should skip lines 1-2."""
        lines = [f"line {i}" for i in range(1, 11)]
        target = workspace_dir / "numbered.txt"
        target.write_text("\n".join(lines) + "\n")

        result = await handler.execute(
            "read_file",
            {
                "path": str(target),
                "offset": 3,
            },
        )

        assert "error" not in result
        assert result.get("offset") == 3
        # Lines 1 and 2 should NOT be in the content
        assert "line 1\n" not in result["content"]
        assert "line 2\n" not in result["content"]
        # Lines 3+ should be present
        assert "line 3" in result["content"]
        assert "line 10" in result["content"]

    async def test_with_limit(self, handler, workspace_dir):
        """max_lines limits the number of lines returned."""
        lines = [f"line {i}" for i in range(1, 101)]
        target = workspace_dir / "long.txt"
        target.write_text("\n".join(lines) + "\n")

        result = await handler.execute(
            "read_file",
            {
                "path": str(target),
                "max_lines": 5,
            },
        )

        assert "error" not in result
        assert result.get("truncated") is True
        assert result.get("lines_returned") == 5
        assert "line 1" in result["content"]
        assert "line 6" not in result["content"]

    async def test_file_not_found(self, handler, workspace_dir):
        """Error for nonexistent file."""
        result = await handler.execute(
            "read_file",
            {
                "path": str(workspace_dir / "ghost.txt"),
            },
        )

        assert "error" in result
        assert "not found" in result["error"].lower() or "File not found" in result["error"]

    async def test_binary_file(self, handler, workspace_dir):
        """Error for binary files."""
        target = workspace_dir / "binary.bin"
        target.write_bytes(b"\x00\x01\x02\xff\xfe\xfd" * 100)

        result = await handler.execute(
            "read_file",
            {
                "path": str(target),
            },
        )

        assert "error" in result
        assert "binary" in result["error"].lower() or "Binary" in result["error"]


# ---------------------------------------------------------------------------
# list_directory tests
# ---------------------------------------------------------------------------


class TestListDirectory:
    async def test_tool_exists(self, handler, project_with_workspace, workspace_dir):
        """Verify tool def + handler work together for a basic listing."""
        project_id, ws_path = project_with_workspace

        # Create some files and dirs
        (workspace_dir / "src").mkdir()
        (workspace_dir / "README.md").write_text("# Hello")
        (workspace_dir / "main.py").write_text("print('hi')")

        handler.set_active_project(project_id)
        result = await handler.execute(
            "list_directory",
            {
                "project_id": project_id,
            },
        )

        assert "error" not in result
        assert result["project_id"] == project_id
        assert "src" in result["directories"]
        file_names = [f["name"] for f in result["files"]]
        assert "README.md" in file_names
        assert "main.py" in file_names

    async def test_subdirectory(self, handler, project_with_workspace, workspace_dir):
        """List a subdirectory within the workspace."""
        project_id, ws_path = project_with_workspace
        sub = workspace_dir / "src"
        sub.mkdir()
        (sub / "app.py").write_text("# app")

        handler.set_active_project(project_id)
        result = await handler.execute(
            "list_directory",
            {
                "project_id": project_id,
                "path": "src",
            },
        )

        assert "error" not in result
        file_names = [f["name"] for f in result["files"]]
        assert "app.py" in file_names

    async def test_no_project(self, handler):
        """Error when no project_id and no active project."""
        result = await handler.execute("list_directory", {})

        assert "error" in result
        assert "project_id" in result["error"].lower() or "required" in result["error"].lower()


# ---------------------------------------------------------------------------
# write_file tests
# ---------------------------------------------------------------------------


class TestWriteFile:
    async def test_basic_write(self, handler, workspace_dir):
        """Write content to a new file."""
        target = workspace_dir / "output.txt"

        result = await handler.execute(
            "write_file",
            {
                "path": str(target),
                "content": "hello world",
            },
        )

        assert "error" not in result
        assert result["written"] == len("hello world")
        assert target.read_text() == "hello world"

    async def test_overwrite(self, handler, workspace_dir):
        """Overwrite an existing file."""
        target = workspace_dir / "existing.txt"
        target.write_text("old content")

        result = await handler.execute(
            "write_file",
            {
                "path": str(target),
                "content": "new content",
            },
        )

        assert "error" not in result
        assert target.read_text() == "new content"

    async def test_creates_parent_dirs(self, handler, workspace_dir):
        """Creates parent directories if they don't exist."""
        target = workspace_dir / "deep" / "nested" / "file.txt"

        result = await handler.execute(
            "write_file",
            {
                "path": str(target),
                "content": "deep content",
            },
        )

        assert "error" not in result
        assert target.read_text() == "deep content"


# ---------------------------------------------------------------------------
# Path validation (security boundary) tests
# ---------------------------------------------------------------------------


class TestPathValidation:
    """Ensure all file tools respect _validate_path() sandbox."""

    async def test_edit_file_outside_workspace(self, handler, tmp_path):
        """edit_file rejects paths outside workspace."""
        # Create a file outside the workspace
        outside = tmp_path / "outside.txt"
        outside.write_text("sensitive data")

        result = await handler.execute(
            "edit_file",
            {
                "path": str(outside),
                "old_string": "sensitive",
                "new_string": "redacted",
            },
        )

        assert "error" in result
        assert "denied" in result["error"].lower() or "Access denied" in result["error"]
        # File should be unchanged
        assert outside.read_text() == "sensitive data"

    async def test_read_file_outside_workspace(self, handler, tmp_path):
        """read_file rejects paths outside workspace."""
        outside = tmp_path / "secret.txt"
        outside.write_text("top secret")

        result = await handler.execute(
            "read_file",
            {
                "path": str(outside),
            },
        )

        assert "error" in result
        assert "denied" in result["error"].lower() or "Access denied" in result["error"]

    async def test_write_file_outside_workspace(self, handler, tmp_path):
        """write_file rejects paths outside workspace."""
        result = await handler.execute(
            "write_file",
            {
                "path": str(tmp_path / "evil.txt"),
                "content": "pwned",
            },
        )

        assert "error" in result
        assert "denied" in result["error"].lower() or "Access denied" in result["error"]

    async def test_glob_files_outside_workspace(self, handler, tmp_path):
        """glob_files rejects paths outside workspace."""
        result = await handler.execute(
            "glob_files",
            {
                "pattern": "*.py",
                "path": str(tmp_path),
            },
        )

        assert "error" in result
        assert "denied" in result["error"].lower() or "Access denied" in result["error"]

    async def test_grep_outside_workspace(self, handler, tmp_path):
        """grep rejects paths outside workspace."""
        result = await handler.execute(
            "grep",
            {
                "pattern": "password",
                "path": str(tmp_path),
            },
        )

        assert "error" in result
        assert "denied" in result["error"].lower() or "Access denied" in result["error"]

    async def test_traversal_attack(self, handler, workspace_dir, tmp_path):
        """Path traversal (../) should be blocked."""
        # Create a file adjacent to the workspace
        (tmp_path / "adjacent.txt").write_text("should not read")

        # Try to escape via ../
        result = await handler.execute(
            "read_file",
            {
                "path": str(workspace_dir / ".." / ".." / "adjacent.txt"),
            },
        )

        assert "error" in result
        assert "denied" in result["error"].lower() or "Access denied" in result["error"]


# ---------------------------------------------------------------------------
# Tool registry — files category
# ---------------------------------------------------------------------------


class TestFilesCategoryRegistry:
    def _registry_with_plugins(self):
        """Create a ToolRegistry with plugin tools included."""
        from unittest.mock import MagicMock
        from src.tools import ToolRegistry, _ALL_TOOL_DEFINITIONS

        mock_pr = MagicMock()
        mock_pr.get_all_tool_definitions.return_value = [
            {"name": n, "description": f"Tool: {n}",
             "input_schema": {"type": "object", "properties": {}}, "_category": "files"}
            for n in ["read_file", "write_file", "edit_file", "glob_files",
                       "grep", "search_files", "list_directory", "run_command"]
        ]
        reg = ToolRegistry(tools=list(_ALL_TOOL_DEFINITIONS))
        reg.set_plugin_registry(mock_pr)
        return reg

    def test_files_category_exists(self):
        """The 'files' category should exist in the tool registry."""
        registry = self._registry_with_plugins()
        categories = registry.get_categories()
        cat_names = {c["name"] for c in categories}
        assert "files" in cat_names

    def test_files_category_tools(self):
        """The 'files' category should contain the expected tools."""
        registry = self._registry_with_plugins()
        file_tools = registry.get_category_tools("files")
        assert file_tools is not None

        tool_names = {t["name"] for t in file_tools}
        expected = {
            "read_file",
            "write_file",
            "edit_file",
            "glob_files",
            "grep",
            "search_files",
            "list_directory",
        }
        assert expected.issubset(tool_names), (
            f"Missing tools from files category: {expected - tool_names}"
        )


# ---------------------------------------------------------------------------
# categorize_file / _is_excluded_path / _weighted_integer_split (pure logic)
# ---------------------------------------------------------------------------


class TestCategorizeFile:
    """Pure-logic tests for the file-selection helpers.

    These do not require a handler/database and validate the categorization
    rules used by ``select_files_for_inspection``.
    """

    @pytest.mark.parametrize(
        "path,expected",
        [
            # Source
            ("src/main.py", "source"),
            ("src/foo.rs", "source"),
            ("frontend/app.tsx", "source"),
            ("lib/util.go", "source"),
            # Tests
            ("tests/test_foo.py", "tests"),
            ("test/foo_test.py", "tests"),
            ("src/foo_test.py", "tests"),
            ("frontend/__tests__/app.test.ts", "tests"),
            ("foo.spec.js", "tests"),
            # Specs / docs
            ("docs/design.md", "specs"),
            ("README.md", "specs"),
            ("specs/plan.md", "specs"),
            ("notes/audit.md", "specs"),
            ("CONTRIBUTING.rst", "specs"),
            # Config
            ("pyproject.toml", "config"),
            ("package.json", "config"),
            ("Dockerfile", "config"),
            (".github/workflows/ci.yml", "config"),
            ("config.yaml", "config"),
            ("setup.cfg", "config"),
            # Other
            ("LICENSE", "other"),
            ("foo.unknown", "other"),
        ],
    )
    def test_categorize(self, path, expected):
        from src.plugins.internal.files import categorize_file

        assert categorize_file(path) == expected


class TestIsExcludedPath:
    @pytest.mark.parametrize(
        "path,excluded",
        [
            ("src/foo.py", False),
            ("__pycache__/foo.pyc", True),
            ("node_modules/lib/x.js", True),
            ("dist/bundle.js", True),
            ("build/out.py", True),
            ("image.png", True),
            ("icon.svg", True),
            (".git/HEAD", True),
            # Known config lockfiles are kept
            ("package-lock.json", False),
            ("yarn.lock", False),
            ("uv.lock", False),
            # Generic *.lock files are excluded
            ("foo.lock", True),
            # Nested node_modules
            ("packages/foo/node_modules/x.js", True),
            # Regular source
            ("src/plugins/files.py", False),
        ],
    )
    def test_exclusion(self, path, excluded):
        from src.plugins.internal.files import _is_excluded_path

        assert _is_excluded_path(path) is excluded


class TestWeightedIntegerSplit:
    def test_sums_to_total(self):
        from src.plugins.internal.files import _weighted_integer_split

        weights = {
            "source": 0.40,
            "specs": 0.20,
            "tests": 0.15,
            "config": 0.10,
            "recent": 0.15,
        }
        for total in (1, 3, 5, 10, 17, 100):
            split = _weighted_integer_split(weights, total)
            assert sum(split.values()) == total
            assert set(split.keys()) == set(weights.keys())

    def test_zero_total(self):
        from src.plugins.internal.files import _weighted_integer_split

        weights = {"a": 0.5, "b": 0.5}
        assert _weighted_integer_split(weights, 0) == {"a": 0, "b": 0}

    def test_dominant_weight(self):
        from src.plugins.internal.files import _weighted_integer_split

        # Single dominant category should receive all remainder
        split = _weighted_integer_split(
            {"source": 0.9, "specs": 0.05, "tests": 0.05}, 1
        )
        assert split["source"] == 1
        assert split["specs"] == 0
        assert split["tests"] == 0


# ---------------------------------------------------------------------------
# select_files_for_inspection / record_file_inspection
# ---------------------------------------------------------------------------


class TestSelectFilesForInspection:
    async def _seed_workspace(self, workspace_dir):
        """Create a small representative file tree in the workspace."""
        # Source
        (workspace_dir / "src").mkdir()
        for name in ("main.py", "auth.py", "db.py", "util.py"):
            (workspace_dir / "src" / name).write_text(f"# {name}\n")
        # Tests
        (workspace_dir / "tests").mkdir()
        for name in ("test_main.py", "test_auth.py"):
            (workspace_dir / "tests" / name).write_text(f"# {name}\n")
        # Specs/docs
        (workspace_dir / "docs").mkdir()
        (workspace_dir / "docs" / "overview.md").write_text("# overview\n")
        (workspace_dir / "README.md").write_text("# project\n")
        # Config
        (workspace_dir / "pyproject.toml").write_text("[project]\nname='x'\n")
        (workspace_dir / "Dockerfile").write_text("FROM python:3.12\n")
        # Excluded
        (workspace_dir / "__pycache__").mkdir()
        (workspace_dir / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")
        (workspace_dir / "node_modules").mkdir()
        (workspace_dir / "node_modules" / "lib.js").write_text("// skip\n")
        (workspace_dir / "logo.png").write_bytes(b"\x89PNG\r\n")

    async def test_basic_selection(
        self, handler, project_with_workspace, workspace_dir
    ):
        """Tool returns a selection respecting count and skipping excluded files."""
        project_id, _ = project_with_workspace
        await self._seed_workspace(workspace_dir)

        handler.set_active_project(project_id)
        result = await handler.execute(
            "select_files_for_inspection",
            {
                "project_id": project_id,
                "count": 5,
                "history_lookback_days": 0,  # disable history lookup
                "seed": 42,
            },
        )

        assert "error" not in result, result
        assert result["project_id"] == project_id
        assert isinstance(result["files"], list)
        assert 1 <= len(result["files"]) <= 5

        # Excluded files should never appear
        for f in result["files"]:
            assert "__pycache__" not in f
            assert "node_modules" not in f
            assert not f.endswith(".png")

        # Every selected file should appear in exactly one category breakdown
        flat = [p for plist in result["categorized"].values() for p in plist]
        assert set(flat) == set(result["files"])

        assert result["total_enumerated"] >= len(result["files"])

    async def test_deterministic_with_seed(
        self, handler, project_with_workspace, workspace_dir
    ):
        """Same seed must produce the same selection."""
        project_id, _ = project_with_workspace
        await self._seed_workspace(workspace_dir)
        handler.set_active_project(project_id)

        r1 = await handler.execute(
            "select_files_for_inspection",
            {"project_id": project_id, "count": 4, "seed": 7,
             "history_lookback_days": 0},
        )
        r2 = await handler.execute(
            "select_files_for_inspection",
            {"project_id": project_id, "count": 4, "seed": 7,
             "history_lookback_days": 0},
        )
        assert r1["files"] == r2["files"]

    async def test_weighted_distribution(
        self, handler, project_with_workspace, workspace_dir
    ):
        """With enough files in each category, targets should be met."""
        project_id, _ = project_with_workspace
        # Generate a large tree so each category has enough candidates
        (workspace_dir / "src").mkdir()
        (workspace_dir / "tests").mkdir()
        (workspace_dir / "docs").mkdir()
        for i in range(20):
            (workspace_dir / "src" / f"mod{i}.py").write_text(f"# {i}\n")
            (workspace_dir / "tests" / f"test_mod{i}.py").write_text(f"# {i}\n")
            (workspace_dir / "docs" / f"doc{i}.md").write_text(f"# {i}\n")
        # A handful of config files
        (workspace_dir / "pyproject.toml").write_text("")
        (workspace_dir / "Dockerfile").write_text("")
        (workspace_dir / "config.yaml").write_text("")

        handler.set_active_project(project_id)
        result = await handler.execute(
            "select_files_for_inspection",
            {
                "project_id": project_id,
                "count": 20,
                "history_lookback_days": 0,
                "seed": 1,
            },
        )

        assert "error" not in result, result
        # Total selected should equal requested count
        assert len(result["files"]) == 20
        # Weight-derived target counts should sum to count
        assert sum(result["target_counts"].values()) == 20

    async def test_history_exclusion(
        self, handler, project_with_workspace, workspace_dir, monkeypatch
    ):
        """Files recorded in project memory within lookback window are excluded."""
        project_id, _ = project_with_workspace
        await self._seed_workspace(workspace_dir)
        handler.set_active_project(project_id)

        # Fake memory_kv_list to return one recent inspection of src/main.py
        import time as _time

        recent_ts = int(_time.time()) - 3600  # 1 hour ago
        fake_entries = [
            {
                "namespace": "inspections",
                "key": "src:main.py",
                "value": f'{{"file": "src/main.py", "timestamp": {recent_ts}, "summary": "ok"}}',
            }
        ]

        original_execute = handler.execute

        async def fake_execute(name, args=None, **kwargs):
            if name == "memory_kv_list":
                return {"entries": fake_entries}
            return await original_execute(name, args, **kwargs)

        # Patch the FilesPlugin context's command executor to intercept
        # memory calls. The plugin is registered as "aq-files".
        registry = handler.orchestrator.plugin_registry
        loaded = registry._plugins.get("aq-files")
        assert loaded is not None

        async def fake_kv_list(nm, ag):
            if nm == "memory_kv_list":
                return {"entries": fake_entries}
            return {"error": "not found"}

        loaded.context._execute_command_callback = fake_kv_list

        result = await handler.execute(
            "select_files_for_inspection",
            {
                "project_id": project_id,
                "count": 20,  # ask for everything
                "history_lookback_days": 30,
                "seed": 5,
            },
        )

        assert "error" not in result, result
        assert "src/main.py" not in result["files"]
        assert result["excluded_history"] >= 1
        assert "src/main.py" in result["history_files"]

    async def test_recent_category(
        self, handler, project_with_workspace, workspace_dir
    ):
        """Recently modified files are eligible for the 'recent' category."""
        import os as _os
        import time as _time

        project_id, _ = project_with_workspace
        # Build a tree; freshly touched file gets current mtime, others backdated.
        (workspace_dir / "src").mkdir()
        old_file = workspace_dir / "src" / "old.py"
        old_file.write_text("# old\n")
        far_past = _time.time() - (60 * 86400)
        _os.utime(old_file, (far_past, far_past))

        fresh = workspace_dir / "src" / "fresh.py"
        fresh.write_text("# fresh\n")  # defaults to now

        handler.set_active_project(project_id)
        result = await handler.execute(
            "select_files_for_inspection",
            {
                "project_id": project_id,
                "count": 2,
                "recent_days": 7,
                "history_lookback_days": 0,
                "weights": {"recent": 1.0},
                "seed": 0,
            },
        )

        assert "error" not in result, result
        # With weight fully on 'recent', only 'fresh.py' qualifies
        assert "src/fresh.py" in result["files"]


class TestRecordFileInspection:
    async def test_records_via_memory(
        self, handler, project_with_workspace, workspace_dir
    ):
        """record_file_inspection round-trips through memory_kv_set."""
        project_id, _ = project_with_workspace
        handler.set_active_project(project_id)

        # Patch the FilesPlugin context to capture the memory call.
        # The files plugin is registered under "aq-files".
        registry = handler.orchestrator.plugin_registry
        loaded = registry._plugins.get("aq-files")
        assert loaded is not None

        captured: dict = {}

        async def fake_exec(name, args):
            captured["name"] = name
            captured["args"] = args
            return {"ok": True}

        loaded.context._execute_command_callback = fake_exec

        result = await handler.execute(
            "record_file_inspection",
            {
                "project_id": project_id,
                "file_path": "src/main.py",
                "summary": "reviewed",
                "findings_count": 2,
                "category": "source",
            },
        )

        assert "error" not in result, result
        assert result["recorded"] is True
        assert result["project_id"] == project_id
        assert result["file_path"] == "src/main.py"
        assert result["record"]["findings_count"] == 2
        assert result["record"]["summary"] == "reviewed"
        assert result["record"]["category"] == "source"

        # Memory was invoked with the sanitized key + namespace
        assert captured["name"] == "memory_kv_set"
        args = captured["args"]
        assert args["project_id"] == project_id
        assert args["namespace"] == "inspections"
        assert args["key"] == "src:main.py"
        import json as _json

        stored = _json.loads(args["value"])
        assert stored["file"] == "src/main.py"
        assert stored["summary"] == "reviewed"
        assert stored["findings_count"] == 2

    async def test_missing_file_path(self, handler, project_with_workspace):
        project_id, _ = project_with_workspace
        handler.set_active_project(project_id)
        result = await handler.execute(
            "record_file_inspection",
            {"project_id": project_id},
        )
        assert "error" in result
        assert "file_path" in result["error"]

    async def test_memory_error_returns_warning(
        self, handler, project_with_workspace, workspace_dir
    ):
        """If memory_kv_set fails, we surface a warning but don't raise."""
        project_id, _ = project_with_workspace
        handler.set_active_project(project_id)
        registry = handler.orchestrator.plugin_registry
        loaded = registry._plugins.get("aq-files")
        assert loaded is not None

        async def fake_exec(name, args):
            return {"error": "memory unavailable"}

        loaded.context._execute_command_callback = fake_exec

        result = await handler.execute(
            "record_file_inspection",
            {"project_id": project_id, "file_path": "src/main.py"},
        )
        assert result["recorded"] is False
        assert "warning" in result


# ---------------------------------------------------------------------------
# Project memory vault access (read_project_memory_file /
# count_project_memory_files)
# ---------------------------------------------------------------------------


class TestReadProjectMemoryFile:
    """Verify the scoped vault-memory reader exposed to the
    memory-consolidation playbook.

    The ordinary read_file handler sandboxes paths to the workspace; the
    system vault at ``{data_dir}/vault/projects/<id>/memory/`` lives outside
    that sandbox.  ``read_project_memory_file`` resolves paths inside the
    project's memory directory, preventing traversal escape while allowing
    the playbook to read consolidation markers and insight files.
    """

    def _memory_dir(self, config, project_id: str):
        import os as _os

        return _os.path.join(
            config.data_dir, "vault", "projects", project_id, "memory"
        )

    async def test_reads_existing_file(self, handler, config):
        import os as _os

        project_id = "memory-reader-proj"
        mem_dir = self._memory_dir(config, project_id)
        _os.makedirs(mem_dir, exist_ok=True)
        target = _os.path.join(mem_dir, "consolidation.md")
        with open(target, "w") as f:
            f.write("---\nlast_consolidated: 2026-04-01T00:00:00Z\n---\nhello\n")

        result = await handler.execute(
            "read_project_memory_file",
            {"project_id": project_id, "path": "consolidation.md"},
        )

        assert "error" not in result, result
        assert result["project_id"] == project_id
        assert "last_consolidated" in result["content"]
        assert result["path"].endswith("consolidation.md")
        assert result.get("missing") is not True

    async def test_missing_file_returns_flagged_error(self, handler, config):
        project_id = "memory-reader-proj"
        # No file created.
        result = await handler.execute(
            "read_project_memory_file",
            {"project_id": project_id, "path": "consolidation.md"},
        )

        assert result.get("missing") is True
        assert "error" in result

    async def test_rejects_traversal_in_path(self, handler, config):
        import os as _os

        project_id = "traversal-proj"
        mem_dir = self._memory_dir(config, project_id)
        _os.makedirs(mem_dir, exist_ok=True)
        # Create something one level outside memory/
        outside = _os.path.join(_os.path.dirname(mem_dir), "secret.md")
        with open(outside, "w") as f:
            f.write("do not read")

        result = await handler.execute(
            "read_project_memory_file",
            {"project_id": project_id, "path": "../secret.md"},
        )

        assert "error" in result
        assert (
            "denied" in result["error"].lower()
            or "invalid" in result["error"].lower()
            or "outside" in result["error"].lower()
        )

    async def test_rejects_traversal_in_project_id(self, handler):
        """project_id must not contain path-traversal characters."""
        result = await handler.execute(
            "read_project_memory_file",
            {"project_id": "../other", "path": "consolidation.md"},
        )
        assert "error" in result

    async def test_reads_nested_insight_file(self, handler, config):
        import os as _os

        project_id = "nested-proj"
        mem_dir = self._memory_dir(config, project_id)
        insights = _os.path.join(mem_dir, "insights")
        _os.makedirs(insights, exist_ok=True)
        with open(_os.path.join(insights, "insight-a.md"), "w") as f:
            f.write("# insight A\n")

        result = await handler.execute(
            "read_project_memory_file",
            {"project_id": project_id, "path": "insights/insight-a.md"},
        )
        assert "error" not in result, result
        assert "insight A" in result["content"]


class TestCountProjectMemoryFiles:
    def _memory_dir(self, config, project_id: str):
        import os as _os

        return _os.path.join(
            config.data_dir, "vault", "projects", project_id, "memory"
        )

    async def test_counts_all_when_newer_than_omitted(self, handler, config):
        import os as _os

        project_id = "count-proj"
        insights = _os.path.join(self._memory_dir(config, project_id), "insights")
        _os.makedirs(insights, exist_ok=True)
        for name in ("a.md", "b.md", "c.md"):
            with open(_os.path.join(insights, name), "w") as f:
                f.write(f"# {name}\n")

        result = await handler.execute(
            "count_project_memory_files",
            {"project_id": project_id, "path": "insights"},
        )
        assert "error" not in result, result
        assert result["count"] == 3
        assert result["total"] == 3

    async def test_counts_newer_than_iso(self, handler, config):
        import os as _os
        import time as _time

        project_id = "count-proj-time"
        insights = _os.path.join(self._memory_dir(config, project_id), "insights")
        _os.makedirs(insights, exist_ok=True)

        old = _os.path.join(insights, "old.md")
        with open(old, "w") as f:
            f.write("# old\n")
        far_past = _time.time() - (30 * 86400)
        _os.utime(old, (far_past, far_past))

        new = _os.path.join(insights, "new.md")
        with open(new, "w") as f:
            f.write("# new\n")
        # `new` keeps its default mtime (now)

        # Cutoff: 7 days ago — `old` should be excluded, `new` included.
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=7)).isoformat()
        result = await handler.execute(
            "count_project_memory_files",
            {
                "project_id": project_id,
                "path": "insights",
                "newer_than": cutoff,
            },
        )
        assert "error" not in result, result
        assert result["count"] == 1
        assert result["total"] == 2

    async def test_missing_directory_returns_zero(self, handler, config):
        project_id = "count-missing"
        # Do not create the directory.
        result = await handler.execute(
            "count_project_memory_files",
            {"project_id": project_id, "path": "insights"},
        )
        assert "error" not in result, result
        assert result["count"] == 0
        assert result.get("missing") is True

    async def test_rejects_traversal(self, handler, config):
        import os as _os

        project_id = "count-traversal"
        _os.makedirs(self._memory_dir(config, project_id), exist_ok=True)
        result = await handler.execute(
            "count_project_memory_files",
            {"project_id": project_id, "path": "../"},
        )
        assert "error" in result

    async def test_rejects_bad_project_id(self, handler, config):
        result = await handler.execute(
            "count_project_memory_files",
            {"project_id": "../escape", "path": "insights"},
        )
        assert "error" in result

    async def test_ignores_subdirectories_in_count(self, handler, config):
        """Only plain files count — nested dirs don't inflate the total."""
        import os as _os

        project_id = "count-dir-filter"
        insights = _os.path.join(self._memory_dir(config, project_id), "insights")
        _os.makedirs(_os.path.join(insights, "nested"), exist_ok=True)
        with open(_os.path.join(insights, "a.md"), "w") as f:
            f.write("# a\n")

        result = await handler.execute(
            "count_project_memory_files",
            {"project_id": project_id, "path": "insights"},
        )
        assert "error" not in result, result
        assert result["count"] == 1
        assert result["total"] == 1
