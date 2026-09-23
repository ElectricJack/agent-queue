"""Ratchets for the migrated repository and onboarding GitHub surfaces.

The App bootstrap in ``github_app.py`` and isolated Git's credential helper
are documented exceptions. The legacy ``GitPlugin.create_github_repo`` path
still needs separate removal before a whole-tree single-launcher ratchet can
be enforced.
"""

from __future__ import annotations

import ast
from pathlib import Path

from src.git.github import GitHubAccess, GitHubClient
from src.git.github_cli import GhRunner
from src.projects.github import GhClient


ROOT = Path(__file__).parents[1] / "src"
GITHUB_SURFACES = (
    ROOT / "git" / "github.py",
    ROOT / "projects" / "github.py",
    ROOT / "commands" / "git_commands.py",
    ROOT / "commands" / "ci_commands.py",
    ROOT / "commands" / "project_onboarding_commands.py",
)


def test_repository_client_and_onboarding_share_the_runner_contract() -> None:
    assert not hasattr(__import__("src.git.github", fromlist=["GitHubCLIClient"]), "GitHubCLIClient")
    assert not hasattr(__import__("src.git.github", fromlist=["GitHubAppClient"]), "GitHubAppClient")
    assert GitHubAccess.from_config().runner.__class__ is GhRunner
    assert isinstance(GhClient().access, GitHubAccess)
    assert hasattr(GitHubClient, "credential_identity")


def test_github_business_surfaces_do_not_launch_another_process_or_http_client() -> None:
    forbidden_imports = {"requests", "httpx", "aiohttp", "urllib.request"}
    forbidden_calls = {
        "subprocess.run", "subprocess.Popen", "asyncio.create_subprocess_exec",
        "asyncio.create_subprocess_shell", "os.system",
    }
    violations = []
    for path in GITHUB_SURFACES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module}
            else:
                names = set()
            for name in names & forbidden_imports:
                violations.append(f"{path.name}:{node.lineno}: import {name}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                owner = node.func.value
                if isinstance(owner, ast.Name):
                    name = f"{owner.id}.{node.func.attr}"
                    if name in forbidden_calls:
                        violations.append(f"{path.name}:{node.lineno}: {name}")
    assert violations == []
