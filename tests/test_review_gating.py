"""Document-review gates on downstream task and generic approval surfaces."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.models import Project


@pytest.fixture
async def handler(command_handler_factory):
    h = await command_handler_factory()
    await h.db.create_project(Project(id="p", name="Project P"))
    try:
        yield h
    finally:
        if hasattr(h, "_db") and h._db is not None:
            await h._db.close()


@pytest.fixture
async def submit_review(handler):
    async def _submit(*, project_id: str = "p") -> dict:
        result = await handler.execute(
            "review_submit",
            {
                "project_id": project_id,
                "kind": "spec",
                "title": "Reviewable document",
                "content": "# Reviewable document\n",
            },
        )
        assert result["success"] is True, result
        return result

    return _submit


async def test_after_review_blocks_until_approved(handler, submit_review):
    review = await submit_review()
    created = await handler.execute(
        "create_task",
        {
            "project_id": "p",
            "title": "Implement the review",
            "after_review": review["review_id"],
        },
    )
    assert created["success"] is True, created
    assert (await handler.db.get_task(created["task_id"])).is_blocked is True

    decided = await handler.execute(
        "review_decide",
        {"review_id": review["review_id"], "revision": 1, "decision": "approve"},
    )
    assert decided["success"] is True, decided
    assert (await handler.db.get_task(created["task_id"])).is_blocked is False


async def test_edit_after_review_blocks_until_approved(handler, submit_review):
    review = await submit_review()
    created = await handler.execute(
        "create_task", {"project_id": "p", "title": "Implement the review"}
    )

    edited = await handler.execute(
        "edit_task", {"task_id": created["task_id"], "after_review": review["review_id"]}
    )
    assert edited["fields"] == ["after_review"]
    assert (await handler.db.get_task(created["task_id"])).is_blocked is True

    assert (
        await handler.execute(
            "review_decide",
            {"review_id": review["review_id"], "revision": 1, "decision": "approve"},
        )
    )["success"] is True
    assert (await handler.db.get_task(created["task_id"])).is_blocked is False


async def test_after_review_on_approved_review_does_not_block(handler, submit_review):
    review = await submit_review()
    assert (
        await handler.execute(
            "review_decide",
            {"review_id": review["review_id"], "revision": 1, "decision": "approve"},
        )
    )["success"] is True

    created = await handler.execute(
        "create_task",
        {
            "project_id": "p",
            "title": "Implement the approved review",
            "after_review": review["review_id"],
        },
    )
    assert created["success"] is True, created
    assert (await handler.db.get_task(created["task_id"])).is_blocked is False


async def test_after_review_refuses_withdrawn_and_unknown(handler, submit_review):
    withdrawn = await submit_review()
    assert (
        await handler.execute(
            "review_withdraw", {"review_id": withdrawn["review_id"], "reason": "Superseded"}
        )
    )["success"] is True

    closed = await handler.execute(
        "create_task",
        {
            "project_id": "p",
            "title": "Do not attach to withdrawn",
            "after_review": withdrawn["review_id"],
        },
    )
    assert closed["success"] is False
    assert closed["error_code"] == "review_closed"

    missing = await handler.execute(
        "create_task",
        {"project_id": "p", "title": "Do not attach to unknown", "after_review": "rev-missing"},
    )
    assert missing["success"] is False
    assert missing["error_code"] == "not_found"


async def test_gate_resolve_refuses_review_gates(handler, submit_review):
    review = await submit_review()
    result = await handler.execute(
        "gate_resolve",
        {
            "gate_id": review["gate_id"],
            "resolved_by": "dashboard",
            "resolution": "approved",
        },
    )
    assert result["success"] is False
    assert result["error_code"] == "review_gate"
    assert "aq review decide" in result["error"]


async def test_spec_approve_refuses_reviewed_file_and_allows_plain_spec(handler, submit_review):
    review = await submit_review()
    review_row = await handler.db.get_review(review["review_id"])
    reviewed_path = Path(handler.config.vault_root) / review_row["vault_path"]

    refused = await handler.execute(
        "spec_approve", {"project_id": "p", "spec_path": str(reviewed_path)}
    )
    assert refused["success"] is False
    assert refused["error_code"] == "review_gate"
    assert review["review_id"] in refused["error"]

    plain_path = Path(handler.config.vault_root) / "projects" / "p" / "specs" / "plain.md"
    plain_path.parent.mkdir(parents=True, exist_ok=True)
    plain_path.write_text("---\nstatus: draft\n---\n# Plain spec\n", encoding="utf-8")
    approved = await handler.execute(
        "spec_approve", {"project_id": "p", "spec_path": str(plain_path)}
    )
    assert approved["success"] is True, approved
