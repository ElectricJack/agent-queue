"""Generated instructions honor live vault edits without changing observed facts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.commands.ci_commands import render_repair_task
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.integration.checks import Conclusion
from src.integration.root_pr_recovery import RootPRCheckFailure
from src.integration.source_ci import repair_description
from src.plugins.services import WorkspaceServiceImpl
from src.profiles.capabilities import DENY_ALL
from src.task_templates import (
    SHIPPED_TASK_TEMPLATES,
    render_task_template,
    task_template_scope,
)
from src.vault import ensure_default_templates


def installed(tmp_path, name, body):
    path = tmp_path / "vault" / "templates" / "tasks" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_seeding_preserves_operator_edits_and_fills_missing_templates(tmp_path):
    custom = installed(tmp_path, "source-ci-repair", "Operator repair wording {{task_id}}")
    first = ensure_default_templates(str(tmp_path))
    assert "tasks/source-ci-repair.md" in first["skipped"]
    assert "tasks/source-ci-reopen.md" in first["created"]
    assert custom.read_text() == "Operator repair wording {{task_id}}"
    for default in SHIPPED_TASK_TEMPLATES.glob("*.md"):
        assert (custom.parent / default.name).is_file()
    missing = custom.parent / "root-pr-check-repair.md"
    missing.unlink()
    second = ensure_default_templates(str(tmp_path))
    assert second["created"] == ["tasks/root-pr-check-repair.md"]
    assert custom.read_text() == "Operator repair wording {{task_id}}"


def test_source_ci_builder_reads_each_edit_and_preserves_exact_facts(tmp_path):
    observation = SimpleNamespace(
        task_id="source-task",
        state="red",
        policy_generation=7,
        source={
            "pr_url": "https://example/pr/3",
            "repository_id": "repo",
            "branch": "aq/source",
            "base": "a" * 40,
            "head": "b" * 40,
            "generation": 9,
        },
        evidence={
            "checks": [
                {
                    "name": "required",
                    "conclusion": "failure",
                    "url": "https://example/check",
                    "summary": "FAILED {{literal}}",
                },
                {"name": "passing", "conclusion": "success"},
            ],
            "failing_checks": ["required"],
        },
    )
    path = installed(
        tmp_path,
        "source-ci-reopen",
        "First {{task_id}}: {{source_pr_url}} {{source_base}}..{{source_head}}\n{{checks}}",
    )
    legacy = installed(tmp_path, "source-ci-repair", "Legacy independent branch instructions")
    with task_template_scope(tmp_path):
        first = repair_description(observation)
        assert first.startswith("First source-task: https://example/pr/3 " + "a" * 40)
        assert "b" * 40 in first
        assert "FAILED {{literal}}" in first
        assert "passing" not in first
        path.write_text("Changed {{task_id}}\n{{checks}}")
        assert repair_description(observation).startswith("Changed source-task\n")
        path.unlink()
        assert "Authorized source CI repair" in repair_description(observation)
        assert "existing PR" in repair_description(observation)
        assert "Legacy independent" not in repair_description(observation)
        assert legacy.read_text() == "Legacy independent branch instructions"


def test_root_pr_recovery_uses_live_instructions_and_only_actual_failures(tmp_path):
    installed(
        tmp_path,
        "root-pr-check-repair",
        "{{source_base_sha}}..{{source_sha}} {{pr_url}}\n{{checks}}\n"
        "Operator instructions: use focused checks.",
    )
    failed = SimpleNamespace(
        conclusion=Conclusion.FAILURE,
        name="tests",
        run_url="https://example/run",
        detail={"failing_test_ids": ["test_x"]},
    )
    passed = SimpleNamespace(conclusion=Conclusion.SUCCESS, name="passing")
    observation = RootPRCheckFailure(
        request=None,
        source_sha="b" * 40,
        source_base_sha="a" * 40,
        pr_url="https://example/pr/3",
        policy_generation=1,
        policy={},
        checks=SimpleNamespace(checks=[failed, passed]),
    )
    with task_template_scope(tmp_path):
        feedback = observation.feedback()
    assert "b" * 40 in feedback
    assert "FAILED test_x" in feedback
    assert "passing" not in feedback
    assert feedback.endswith("Operator instructions: use focused checks.")


def test_ci_sentinel_can_replace_its_complete_instruction_body(tmp_path):
    installed(tmp_path, "ci-repair", "Custom {{ref}} attempt {{attempt}}\n{{facts}}")
    with task_template_scope(tmp_path):
        title, description = render_repair_task(
            ref="main",
            head_sha="c" * 40,
            failing_checks=["unit"],
            failing_tests=["test_failure"],
            run_url="https://example/run",
            attempt=2,
        )
    assert "attempt 2" in title
    assert description.startswith("Custom main attempt 2\n")
    assert "test_failure" in description
    assert "c" * 40 in description


@pytest.mark.parametrize(
    "body,error",
    [
        ("Do {{unknown}}", "unknown variables: unknown"),
        ("Do {{invalid-name}}", "malformed placeholders"),
        ("Do {{unfinished", "malformed placeholders"),
    ],
)
def test_invalid_operator_edits_fail_visibly(tmp_path, body, error):
    installed(tmp_path, "aggregate-verifier", body)
    with task_template_scope(tmp_path), pytest.raises(ValueError, match=error):
        render_task_template("aggregate-verifier")


def test_unreadable_edit_is_not_silently_replaced(tmp_path):
    path = installed(tmp_path, "aggregate-verifier", "custom")
    path.write_bytes(b"\xff")
    with task_template_scope(tmp_path), pytest.raises(UnicodeDecodeError):
        render_task_template("aggregate-verifier")


@pytest.mark.parametrize("name", ["../aggregate-verifier", "/tmp/other", "unknown-template"])
def test_only_known_template_names_are_renderable(name):
    with pytest.raises(ValueError):
        render_task_template(name)


async def test_concurrent_vault_scopes_and_child_tasks_are_isolated(tmp_path):
    left, right = tmp_path / "left", tmp_path / "right"
    installed(left, "aggregate-verifier", "left")
    installed(right, "aggregate-verifier", "right")
    barrier = asyncio.Barrier(2)

    async def read_in_child():
        await barrier.wait()
        return render_task_template("aggregate-verifier")

    async def reader(root):
        with task_template_scope(root):
            child = asyncio.create_task(read_in_child())
        return await child

    assert await asyncio.gather(reader(left), reader(right)) == ["left", "right"]
    assert "Verify the exact collected aggregate" in render_task_template("aggregate-verifier")


@pytest.mark.parametrize(
    "kind,elevated,allowed",
    [
        (PrincipalKind.LOCAL, False, True),
        (PrincipalKind.SESSION, True, True),
        (PrincipalKind.SESSION, False, False),
        (PrincipalKind.PLAYBOOK, False, False),
    ],
)
async def test_file_service_template_access_is_limited_to_operators_and_supervisors(
    tmp_path,
    kind,
    elevated,
    allowed,
):
    from unittest.mock import AsyncMock

    db = SimpleNamespace(
        list_repos=AsyncMock(return_value=[]), list_workspaces=AsyncMock(return_value=[])
    )
    config = SimpleNamespace(data_dir=str(tmp_path), workspace_dir=str(tmp_path / "workspace"))
    service = WorkspaceServiceImpl(db, None, config)
    template = installed(tmp_path, "aggregate-verifier", "custom")
    principal = ExecutionPrincipal(kind=kind, policy=DENY_ALL, elevated=elevated)
    with principal_context(principal):
        assert (await service.validate_path(str(template)) is not None) is allowed
        assert (
            await service.validate_path(str(template.parent.parent / "profile-template.md")) is None
        )
        outside = tmp_path / "secret"
        outside.write_text("secret")
        template.unlink()
        template.symlink_to(outside)
        assert await service.validate_path(str(template)) is None
        template.unlink()
        template.parent.rmdir()
        other_directory = tmp_path / "other-vault"
        other_directory.mkdir()
        (other_directory / template.name).write_text("outside")
        template.parent.symlink_to(other_directory, target_is_directory=True)
        assert await service.validate_path(str(template)) is None
