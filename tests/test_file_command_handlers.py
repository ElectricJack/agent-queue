"""Unit tests for filesystem command handlers.

Tests: edit_file, glob_files, grep, read_file (with offset), list_directory,
write_file.  All handlers are tested through handler.execute() with real
temp-directory files and a real SQLite database.  Path validation (sandbox
security) is tested separately at the end.
"""

import os
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.command_handler import CommandHandler
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
        from src.tool_registry import ToolRegistry, _ALL_TOOL_DEFINITIONS

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
