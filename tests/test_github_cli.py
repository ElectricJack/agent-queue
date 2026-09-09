from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from src.git.github_app import GitHubRepositoryBinding
from src.git.github_cli import GitHubCLIClient
from src.git.manager import GitManager


def _fake_gh(
    tmp_path: Path,
    response: object,
    *,
    exit_code: int = 0,
    stderr: str = "",
) -> tuple[str, Path]:
    capture = tmp_path / "gh-call.json"
    executable = tmp_path / "gh"
    executable.write_text(
        "#!/usr/bin/python3\n"
        "import json, os, sys\n"
        "stdin = sys.stdin.read()\n"
        "with open(os.environ['AQ_TEST_GH_CAPTURE'], 'w') as stream:\n"
        "    json.dump({'argv': sys.argv[1:], 'stdin': stdin}, stream)\n"
        f"response = {response!r}\n"
        "pages = response if '--paginate' in sys.argv else [response]\n"
        "for page in pages: print(json.dumps(page, indent=2))\n"
        f"print({json.dumps(stderr)}, file=sys.stderr)\n"
        f"raise SystemExit({exit_code})\n"
    )
    executable.chmod(0o700)
    return str(executable), capture


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _git_push_case(tmp_path: Path) -> tuple[Path, Path, str, str]:
    checkout = tmp_path / "checkout"
    target = tmp_path / "target.git"
    checkout.mkdir()
    _git(["init", "--initial-branch=main"], checkout)
    _git(["config", "user.name", "Test"], checkout)
    _git(["config", "user.email", "test@example.com"], checkout)
    (checkout / "file.txt").write_text("base")
    _git(["add", "file.txt"], checkout)
    _git(["commit", "-m", "base"], checkout)
    base = _git(["rev-parse", "HEAD"], checkout)
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(target)],
        check=True,
        capture_output=True,
    )
    _git(["push", str(target), f"{base}:refs/heads/main"], checkout)
    (checkout / "file.txt").write_text("tip")
    _git(["commit", "-am", "tip"], checkout)
    tip = _git(["rev-parse", "HEAD"], checkout)
    return checkout, target, base, tip


@pytest.mark.asyncio
async def test_bind_repository_uses_authenticated_gh_identity(tmp_path):
    executable, capture = _fake_gh(
        tmp_path,
        {"id": 303, "full_name": "acme/widgets"},
    )

    client = await GitHubCLIClient.bind_repository(
        "acme/widgets",
        executable=executable,
        env={"AQ_TEST_GH_CAPTURE": str(capture)},
    )

    assert client.repository == GitHubRepositoryBinding(303, "acme/widgets")
    assert client.auth_mode == "gh"
    invocation = json.loads(capture.read_text())
    assert invocation["argv"] == [
        "api",
        "--hostname",
        "github.com",
        "--method",
        "GET",
        "repos/acme/widgets",
    ]
    assert invocation["stdin"] == ""


@pytest.mark.asyncio
async def test_request_json_passes_body_on_stdin_not_process_arguments(tmp_path):
    executable, capture = _fake_gh(tmp_path, {"id": 901})
    client = GitHubCLIClient(
        GitHubRepositoryBinding(303, "acme/widgets"),
        executable=executable,
        env={"AQ_TEST_GH_CAPTURE": str(capture)},
    )

    result = await client.request_json(
        "POST",
        "/repos/acme/widgets/check-runs",
        json_body={"name": "required", "head_sha": "a" * 40},
    )

    assert result == {"id": 901}
    invocation = json.loads(capture.read_text())
    assert invocation["argv"] == [
        "api",
        "--hostname",
        "github.com",
        "--method",
        "POST",
        "repos/acme/widgets/check-runs",
        "--input",
        "-",
    ]
    assert json.loads(invocation["stdin"]) == {
        "name": "required",
        "head_sha": "a" * 40,
    }
    assert "required" not in " ".join(invocation["argv"])


@pytest.mark.asyncio
async def test_exact_head_ref_uses_numeric_binding_and_escaped_short_ref(tmp_path):
    oid = "a" * 40
    executable, capture = _fake_gh(
        tmp_path,
        {"ref": "refs/heads/feature/x", "object": {"sha": oid}},
    )
    client = GitHubCLIClient(
        GitHubRepositoryBinding(303, "acme/widgets"),
        executable=executable,
        env={"AQ_TEST_GH_CAPTURE": str(capture)},
    )

    assert await client.exact_head_ref("feature/x") == oid
    invocation = json.loads(capture.read_text())
    assert invocation["argv"][-1] == "repositories/303/git/ref/heads/feature%2Fx"


@pytest.mark.asyncio
async def test_paged_items_flattens_successive_json_pages_without_slurp(tmp_path):
    executable, capture = _fake_gh(
        tmp_path,
        [
            {"check_runs": [{"id": 1}]},
            {"check_runs": [{"id": 2}, {"id": 3}]},
        ],
    )
    client = GitHubCLIClient(
        GitHubRepositoryBinding(303, "acme/widgets"),
        executable=executable,
        env={"AQ_TEST_GH_CAPTURE": str(capture)},
    )

    assert await client.paged_items("/repos/acme/widgets/check-runs", key="check_runs") == [
        {"id": 1},
        {"id": 2},
        {"id": 3},
    ]
    invocation = json.loads(capture.read_text())
    assert invocation["argv"][-2:] == ["repos/acme/widgets/check-runs", "--paginate"]


@pytest.mark.asyncio
async def test_repository_helpers_and_installation_token_use_cli_contract(tmp_path):
    executable, capture = _fake_gh(
        tmp_path,
        [
            [{"body": "unrelated"}],
            [{"body": "delivery marker"}],
        ],
    )
    client = GitHubCLIClient(
        GitHubRepositoryBinding(303, "acme/widgets"),
        executable=executable,
        env={"AQ_TEST_GH_CAPTURE": str(capture)},
    )

    assert await client.has_comment_marker(number=7, marker="delivery marker") is True
    assert await client.installation_token() is None


@pytest.mark.asyncio
async def test_not_found_cli_failure_maps_to_absent_exact_head_without_exposing_token(tmp_path):
    secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
    executable, _capture = _fake_gh(
        tmp_path,
        {},
        exit_code=1,
        stderr=f"HTTP 404: missing; Authorization: Bearer {secret}",
    )
    client = GitHubCLIClient(
        GitHubRepositoryBinding(303, "acme/widgets"),
        executable=executable,
        env={"AQ_TEST_GH_CAPTURE": str(tmp_path / "capture")},
    )

    assert await client.exact_head_ref("missing") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "https://api.github.com/repos/acme/widgets",
        "--hostname=attacker.example",
        "/repos/other/repository/pulls",
        "/repositories/999/pulls",
        "/repos/acme/widgets\n--hostname=attacker.example",
    ],
)
async def test_request_json_rejects_unbound_or_option_like_endpoints_before_gh(tmp_path, path):
    executable, capture = _fake_gh(tmp_path, {})
    client = GitHubCLIClient(
        GitHubRepositoryBinding(303, "acme/widgets"),
        executable=executable,
        env={"AQ_TEST_GH_CAPTURE": str(capture)},
    )

    with pytest.raises(ValueError, match="repository-bound"):
        await client.request_json("GET", path)

    assert not capture.exists()


@pytest.mark.asyncio
async def test_cancelling_request_reaps_gh_process(tmp_path):
    pid_file = tmp_path / "gh.pid"
    executable = tmp_path / "gh"
    executable.write_text(
        "#!/usr/bin/python3\n"
        "import os, time\n"
        "with open(os.environ['AQ_TEST_GH_PID'], 'w') as stream:\n"
        "    stream.write(str(os.getpid()))\n"
        "time.sleep(300)\n"
    )
    executable.chmod(0o700)
    client = GitHubCLIClient(
        GitHubRepositoryBinding(303, "acme/widgets"),
        executable=str(executable),
        env={"AQ_TEST_GH_PID": str(pid_file)},
    )
    task = asyncio.create_task(client.request_json("GET", "/repos/acme/widgets"))
    for _ in range(100):
        if pid_file.exists():
            break
        await asyncio.sleep(0.01)
    assert pid_file.exists()
    pid = int(pid_file.read_text())

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not os.path.exists(f"/proc/{pid}")


@pytest.mark.asyncio
async def test_tokenless_exact_push_keeps_oid_and_old_tip_lease(tmp_path):
    checkout, target, base, tip = _git_push_case(tmp_path)

    result = await GitManager()._apush_oid_with_app_auth_to_url(
        str(checkout),
        destination_url=target.as_uri(),
        token=None,
        tip_oid=tip,
        branch="main",
        expected_old_oid=base,
    )

    assert result == tip
    assert _git(["rev-parse", "refs/heads/main"], target) == tip


@pytest.mark.asyncio
async def test_tokenless_exact_fetch_imports_only_requested_oid(tmp_path):
    checkout, source, _base, tip = _git_push_case(tmp_path)
    _git(["push", str(source), f"{tip}:refs/heads/topic"], checkout)
    destination = tmp_path / "retained.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(destination)],
        check=True,
        capture_output=True,
    )

    result = await GitManager()._afetch_exact_oid_with_app_auth_to_url(
        str(destination),
        destination_url=source.as_uri(),
        token=None,
        oid=tip,
        destination_ref="refs/aq/exact/test-tip",
    )

    assert result == tip
    assert _git(["rev-parse", "refs/aq/exact/test-tip"], destination) == tip
    assert _git(["for-each-ref", "--format=%(refname)"], destination).splitlines() == [
        "refs/aq/exact/test-tip"
    ]
