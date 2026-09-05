"""Behaviour tests for configured project-root discovery and browsing."""

from __future__ import annotations

from pathlib import Path

from src.config import ProjectRoot


async def _handler_with_root(command_handler_factory, root: Path):
    handler = await command_handler_factory()
    handler.config.project_roots = [ProjectRoot(id="development", label="Development", path=str(root))]
    return handler


async def test_lists_configured_roots_with_live_capabilities(command_handler_factory, tmp_path):
    root = tmp_path / "development"
    root.mkdir()
    handler = await _handler_with_root(command_handler_factory, root)

    result = await handler.execute("list_project_roots", {})

    assert result == {
        "success": True,
        "roots": [
            {
                "id": "development",
                "label": "Development",
                "path": str(root),
                "readable": True,
                "writable": True,
            }
        ],
    }


async def test_browse_returns_nested_safe_entries_and_hides_dotfiles(command_handler_factory, tmp_path):
    root = tmp_path / "development"
    nested = root / "one" / "two"
    nested.mkdir(parents=True)
    (root / ".hidden").mkdir()
    repository = root / "repository"
    (repository / ".git").mkdir(parents=True)
    (repository / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "plain-file").write_text("not returned as file contents")
    handler = await _handler_with_root(command_handler_factory, root)

    root_result = await handler.execute("browse_project_root", {"root_id": "development"})
    assert root_result["success"] is True
    assert root_result["relative_path"] == ""
    assert [entry["name"] for entry in root_result["entries"]] == [
        "one",
        "plain-file",
        "repository",
    ]
    assert ".hidden" not in {entry["name"] for entry in root_result["entries"]}
    repo_entry = next(entry for entry in root_result["entries"] if entry["name"] == "repository")
    assert repo_entry == {
        "name": "repository",
        "relative_path": "repository",
        "is_directory": True,
        "is_git_repository": True,
        "selectable": True,
    }

    nested_result = await handler.execute(
        "browse_project_root", {"root_id": "development", "relative_path": "one/two"}
    )
    assert nested_result == {
        "success": True,
        "root_id": "development",
        "relative_path": "one/two",
        "entries": [],
        "truncated": False,
    }


async def test_browse_returns_structured_path_failures(command_handler_factory, tmp_path):
    root = tmp_path / "development"
    root.mkdir()
    (root / "file").write_text("not a directory")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    handler = await _handler_with_root(command_handler_factory, root)

    for relative_path, code in [
        ("missing", "not_found"),
        ("file", "not_directory"),
        ("escape", "root_escape"),
    ]:
        result = await handler.execute(
            "browse_project_root", {"root_id": "development", "relative_path": relative_path}
        )
        assert result["success"] is False
        assert result["error_code"] == code


async def test_unknown_or_removed_root_is_unavailable(command_handler_factory, tmp_path):
    root = tmp_path / "development"
    root.mkdir()
    handler = await _handler_with_root(command_handler_factory, root)

    unknown = await handler.execute("browse_project_root", {"root_id": "unknown"})
    assert unknown["error_code"] == "root_unavailable"

    root.rmdir()
    removed = await handler.execute("browse_project_root", {"root_id": "development"})
    assert removed["error_code"] == "root_unavailable"


async def test_project_scoped_caller_cannot_browse_roots(command_handler_factory, tmp_path):
    root = tmp_path / "development"
    root.mkdir()
    handler = await _handler_with_root(command_handler_factory, root)

    result = await handler.execute(
        "browse_project_root",
        {
            "root_id": "development",
            "_scope": {"kind": "session", "elevated": True, "project_id": "project-a"},
        },
    )

    assert result == {
        "success": False,
        "error": "out of scope: project onboarding requires global admin",
        "error_code": "out_of_scope",
    }
