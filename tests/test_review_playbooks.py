"""Playbook reviews: submission pins a compiled artifact, approval stores it.

A playbook review used to be prose only: approving ``rev-sharp-grove`` left no
Playbook V2 artifact anywhere, so nothing could be activated until someone
compiled a bundle by hand.  Now the submission compiles the vault source and
pins the exact artifact hash to the revision, and approval stores exactly that
artifact (and activates it when the review asked), telling the supervisor what
happened either way.  ``reviews.playbook_artifacts`` reports what is left over.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from src.doctor import default_registry
from src.doctor.models import DoctorContext, Severity
from src.doctor.review_checks import PLAYBOOK_CHECK_ID, _check_playbooks, _fix_playbooks
from src.event_bus import EventBus
from src.models import Project
from src.playbooks.definition import load_definition_json
from src.reviews.vault import split_frontmatter

MORNING = Path("tests/fixtures/playbooks/v2/morning-report")
DOCUMENT = "# Playbook V2: morning-report\n\nApprove the policy below.\n"


def _artifact() -> dict:
    return json.loads((MORNING / "artifact.json").read_text(encoding="utf-8"))


def _body(artifact: dict | None = None) -> str:
    artifact = artifact or _artifact()
    return json.dumps({"rules": artifact["rules"], "steps": artifact["steps"]})


@pytest.fixture
async def env(command_handler_factory):
    handler = await command_handler_factory()
    db = handler.db
    handler.config.playbooks.enabled = True
    handler.orchestrator.bus = EventBus(env="dev")
    await db.create_project(Project(id="p", name="Project P"))
    source = Path(handler.config.vault_root) / "system" / "playbooks" / "morning-report.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text((MORNING / "source.md").read_text(encoding="utf-8"), encoding="utf-8")
    try:
        yield handler, db
    finally:
        await db.close()


async def _submit(handler, **overrides) -> dict:
    args = {
        "project_id": "p",
        "title": "Playbook V2: morning-report",
        "content": DOCUMENT,
        "playbook_id": "morning-report",
        "semantic_body": _body(),
        **overrides,
    }
    return await handler.execute(
        "review_submit", {key: value for key, value in args.items() if value is not None}
    )


async def _approve(handler, review_id: str, revision: int = 1) -> dict:
    return await handler.execute(
        "review_decide", {"review_id": review_id, "revision": revision, "decision": "approve"}
    )


def _frontmatter(handler, review: dict) -> dict:
    text = (Path(handler.config.vault_root) / review["vault_path"]).read_text(encoding="utf-8")
    frontmatter, _ = split_frontmatter(text)
    return yaml.safe_load(frontmatter)


# -- submission ---------------------------------------------------------------


async def test_submission_pins_the_compiled_artifact_and_stores_nothing(env):
    handler, db = env
    result = await _submit(handler)
    assert result["success"] is True, result
    pin = result["playbook"]
    assert pin["playbook_id"] == "morning-report"
    assert pin["scope"] == "system"
    assert pin["activate_on_approval"] is False
    assert pin["counts"]["error"] == 0 and pin["counts"]["question"] == 0
    assert pin["source_path"] == "system/playbooks/morning-report.md"

    review = await db.get_review(result["review_id"])
    assert review["kind"] == "other"  # a playbook review defaults to kind other
    revision = await db.get_review_revision(review["id"], 1)
    assert revision["playbook"] == pin
    artifact_bytes = revision["playbook_artifact"].encode("utf-8")
    assert pin["artifact_sha256"] == "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()
    definition = load_definition_json(revision["playbook_artifact"])
    assert definition.id == "morning-report"
    assert definition.source_hash == pin["source_sha256"]

    # An unapproved artifact is never in the store, so it cannot be activated.
    assert await db.get_playbook_artifact_row(pin["artifact_sha256"]) is None
    # The vault copy names the exact hash the reader approves.
    frontmatter = _frontmatter(handler, review)
    assert frontmatter["playbook"] == "morning-report"
    assert frontmatter["artifact_sha256"] == pin["artifact_sha256"]
    assert frontmatter["activate_on_approval"] is False

    shown = await handler.execute("review_show", {"review_id": review["id"]})
    assert shown["revision"]["playbook"] == pin
    assert "playbook_artifact" not in shown["revision"]
    assert all("playbook_artifact" not in row for row in shown["revisions"])


async def test_submission_is_refused_unless_the_artifact_is_activatable(env):
    handler, db = env
    artifact = _artifact()
    artifact["steps"]["reconcile-morning--tick"]["command"] = "no_such_command"
    result = await _submit(handler, semantic_body=_body(artifact))
    assert result["success"] is False
    assert result["error_code"] == "playbook_invalid"
    assert any(row["severity"] == "error" for row in result["diagnostics"])
    assert await db.list_reviews() == []


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"kind": "spec"}, "bad_kind"),
        ({"playbook_id": None}, "playbook_required"),
        ({"playbook_id": "no-such-playbook"}, "playbook_unavailable"),
        ({"semantic_body": None}, "playbook_required"),
        ({"semantic_body": "{\"rules\": [], \"rules\": []}"}, "playbook_invalid"),
        ({"semantic_body_path": "drafts/body.json"}, "playbook_invalid"),
        ({"activate_on_approval": "yes"}, "playbook_invalid"),
    ],
)
async def test_submission_refusals_create_no_review(env, overrides, code):
    handler, db = env
    result = await _submit(handler, **overrides)
    assert result["success"] is False
    assert result["error_code"] == code, result
    assert await db.list_reviews() == []


async def test_submission_reads_a_vault_body_path(env):
    handler, _db = env
    body = Path(handler.config.vault_root) / "drafts" / "morning-report" / "body.json"
    body.parent.mkdir(parents=True)
    body.write_text(_body(), encoding="utf-8")
    result = await _submit(
        handler, semantic_body=None, semantic_body_path="drafts/morning-report/body.json"
    )
    assert result["success"] is True, result
    assert result["playbook"]["playbook_id"] == "morning-report"


async def test_submission_needs_the_compiler(env):
    handler, db = env
    handler.config.playbooks.enabled = False
    result = await _submit(handler)
    assert result["error_code"] == "playbook_unavailable"
    assert await db.list_reviews() == []


async def test_a_plain_review_is_unchanged(env):
    handler, db = env
    result = await handler.execute(
        "review_submit",
        {"project_id": "p", "kind": "spec", "title": "A spec", "content": "# Spec\n"},
    )
    assert result["success"] is True
    assert "playbook" not in result
    revision = await db.get_review_revision(result["review_id"], 1)
    assert revision["playbook"] is None and revision["playbook_artifact"] is None
    assert "playbook" not in _frontmatter(handler, await db.get_review(result["review_id"]))


# -- revisions ------------------------------------------------------------------


async def test_reopening_keeps_the_original_playbook_pin(env):
    handler, db = env
    first = await _submit(handler, activate_on_approval=True)
    rid = first["review_id"]
    original = await db.get_review_revision(rid, 1)
    assert (await handler.execute("review_withdraw", {"review_id": rid}))["success"]
    # Recovery must not recompile policy from a changed source file.
    source = Path(handler.config.vault_root) / "system/playbooks/morning-report.md"
    source.write_text("invalid policy", encoding="utf-8")
    reopened = await handler.execute("review_reopen", {"review_id": rid, "revision": 1})
    assert reopened["success"], reopened
    current = await db.get_review_revision(rid, 2)
    assert current["playbook"] == original["playbook"]
    assert current["playbook_artifact"] == original["playbook_artifact"]
    assert current["content"] == original["content"]


async def _request_changes_directly(db, review_id: str, revision: int = 1) -> None:
    """Move the review to ``changes_requested`` without filing a revision task."""
    async with db.immediate() as conn:
        assert await db.transition_review(
            review_id,
            from_states={"in_review"},
            expected_revision=revision,
            values={"state": "changes_requested"},
            conn=conn,
        )


async def test_a_revision_recompiles_and_pins_again(env):
    handler, db = env
    first = await _submit(handler, activate_on_approval=True)
    review_id = first["review_id"]
    await _request_changes_directly(db, review_id)

    # No body and no playbook id: the pin's playbook and rules/steps are reused
    # against the current source, and activate_on_approval carries forward.
    revised = await handler.execute(
        "review_submit",
        {"review_id": review_id, "content": DOCUMENT + "\nRevised.\n", "changes": "prose"},
    )
    assert revised["success"] is True, revised
    pin = revised["playbook"]
    assert pin["playbook_id"] == "morning-report"
    assert pin["activate_on_approval"] is True
    second = await db.get_review_revision(review_id, 2)
    assert second["playbook"] == pin
    assert load_definition_json(second["playbook_artifact"]).steps.keys() == (
        load_definition_json(
            (await db.get_review_revision(review_id, 1))["playbook_artifact"]
        ).steps.keys()
    )

    await _request_changes_directly(db, review_id, revision=2)
    mismatch = await handler.execute(
        "review_submit",
        {"review_id": review_id, "content": DOCUMENT, "playbook_id": "another-playbook"},
    )
    assert mismatch["error_code"] == "playbook_mismatch"
    assert (await db.get_review(review_id))["current_revision"] == 2


async def test_imported_vault_edits_keep_the_pin(env):
    handler, db = env
    submitted = await _submit(handler)
    review = await db.get_review(submitted["review_id"])
    path = Path(handler.config.vault_root) / review["vault_path"]
    path.write_text(path.read_text(encoding="utf-8") + "\nAn edit in Obsidian.\n", "utf-8")
    imported = await handler.execute("review_import_edits", {"review_id": review["id"]})
    assert imported["success"] is True, imported
    first = await db.get_review_revision(review["id"], 1)
    second = await db.get_review_revision(review["id"], 2)
    assert second["playbook"] == first["playbook"]
    assert second["playbook_artifact"] == first["playbook_artifact"]
    assert _frontmatter(handler, await db.get_review(review["id"]))["artifact_sha256"] == (
        first["playbook"]["artifact_sha256"]
    )


# -- approval -------------------------------------------------------------------


async def test_approval_stores_exactly_the_pinned_artifact_and_tells_the_supervisor(env):
    handler, db = env
    submitted = await _submit(handler)
    sha = submitted["playbook"]["artifact_sha256"]

    decided = await _approve(handler, submitted["review_id"])
    assert decided["success"] is True, decided
    outcome = decided["playbook"]
    assert outcome["stored"] is True
    assert outcome["activated"] is False
    assert outcome["artifact_sha256"] == sha
    assert outcome["next_step"] == (
        f"aq playbook activate --playbook-id morning-report --artifact-sha256 {sha}"
    )

    row = await db.get_playbook_artifact_row(sha)
    assert row is not None and row["playbook_id"] == "morning-report"
    stored = Path(row["path"]).read_bytes()
    pinned = (await db.get_review_revision(submitted["review_id"], 1))["playbook_artifact"]
    assert stored == pinned.encode("utf-8")
    from src.playbooks.artifact_store import ArtifactStore
    assert ArtifactStore(handler.config.compiled_root).load_source(sha) == (
        submitted["playbook"]["source_markdown"]
    )
    provenance = json.loads(row["validation"])["review"]
    assert provenance == {
        "review_id": submitted["review_id"],
        "revision": 1,
        "decided_by": "human:local-operator",
    }
    assert await db.list_playbook_activations() == []

    notices = [
        message
        for message in await db.get_pending_messages("session", "supervisor-p")
        if submitted["review_id"] in message.body
    ]
    assert len(notices) == 1
    assert sha in notices[0].body
    assert outcome["next_step"] in notices[0].body


async def test_approval_activates_when_the_review_asks(env):
    handler, db = env
    submitted = await _submit(handler, activate_on_approval=True)
    sha = submitted["playbook"]["artifact_sha256"]
    decided = await _approve(handler, submitted["review_id"])
    assert decided["playbook"]["stored"] is True
    assert decided["playbook"]["activated"] is True, decided["playbook"]
    activations = await db.list_playbook_activations()
    assert [(row["playbook_id"], row["active_artifact_sha256"]) for row in activations] == [
        ("morning-report", sha)
    ]
    notice = next(
        message
        for message in await db.get_pending_messages("session", "supervisor-p")
        if submitted["review_id"] in message.body
    )
    assert "stored and activated" in notice.body


async def test_approval_stands_when_the_pinned_artifact_cannot_be_stored(env):
    handler, db = env
    submitted = await _submit(handler, activate_on_approval=True)
    review_id = submitted["review_id"]
    # Bytes that no longer hash to the pin: approval must not store them.
    tampered = json.loads((await db.get_review_revision(review_id, 1))["playbook_artifact"])
    tampered["version"] = 99
    from sqlalchemy import update

    from src.database.tables import doc_review_revisions

    async with db.immediate() as conn:
        await conn.execute(
            update(doc_review_revisions)
            .where(doc_review_revisions.c.review_id == review_id)
            .values(playbook_artifact=json.dumps(tampered))
        )

    decided = await _approve(handler, review_id)
    assert decided["success"] is True
    assert (await db.get_review(review_id))["state"] == "approved"
    outcome = decided["playbook"]
    assert outcome["stored"] is False and outcome["activated"] is False
    assert "do not match" in outcome["error"]
    assert await db.get_playbook_artifact_row(submitted["playbook"]["artifact_sha256"]) is None
    assert await db.list_playbook_activations() == []
    notice = next(
        message
        for message in await db.get_pending_messages("session", "supervisor-p")
        if review_id in message.body
    )
    assert "could not be stored" in notice.body


# -- doctor ----------------------------------------------------------------------


def _ctx(handler) -> DoctorContext:
    return DoctorContext(config=handler.config, db=handler.db, handler=handler)


async def test_doctor_lists_unstored_and_unactivated_approved_playbooks(env):
    handler, db = env
    assert PLAYBOOK_CHECK_ID in default_registry().ids()
    submitted = await _submit(handler)
    review_id, sha = submitted["review_id"], submitted["playbook"]["artifact_sha256"]
    ctx = _ctx(handler)

    # In review: nothing to report yet.
    clean = await _check_playbooks(ctx)
    assert clean.severity is Severity.OK and clean.data["checked"] == 0

    # Approved while storing is impossible -> not_stored, and --fix stores it.
    handler.config.playbooks.enabled = False
    decided = await _approve(handler, review_id)
    assert decided["playbook"]["stored"] is False
    handler.config.playbooks.enabled = True
    missing = await _check_playbooks(ctx)
    assert missing.severity is Severity.WARN and missing.fixable is True
    assert [(f["review_id"], f["problem"]) for f in missing.data["findings"]] == [
        (review_id, "not_stored")
    ]

    fixed = await _fix_playbooks(ctx)
    assert fixed.fix_applied is True and fixed.data["stored"] == [review_id]
    assert await db.get_playbook_artifact_row(sha) is not None
    assert await db.list_playbook_activations() == []  # the fix never activates

    # Stored but not live -> not_activated, report-only.
    [finding] = fixed.data["findings"]
    assert finding["problem"] == "not_activated"
    assert finding["next_step"].endswith(sha)
    assert fixed.fixable is False

    activated = await handler.execute(
        "playbook_activate", {"playbook_id": "morning-report", "artifact_sha256": sha}
    )
    assert activated.get("success") is True, activated
    live = await _check_playbooks(ctx)
    assert live.severity is Severity.OK and live.data["checked"] == 1


async def test_doctor_judges_only_the_latest_approval_of_a_playbook(env):
    handler, _db = env
    older = await _submit(handler, title="Playbook V2: morning-report (first)")
    newer = await _submit(handler, title="Playbook V2: morning-report (second)")
    assert (await _approve(handler, older["review_id"]))["playbook"]["stored"] is True
    assert (await _approve(handler, newer["review_id"]))["playbook"]["stored"] is True
    activated = await handler.execute(
        "playbook_activate",
        {
            "playbook_id": "morning-report",
            "artifact_sha256": newer["playbook"]["artifact_sha256"],
        },
    )
    assert activated.get("success") is True, activated
    result = await _check_playbooks(_ctx(handler))
    assert result.severity is Severity.OK, result.data


# -- CLI --------------------------------------------------------------------------


def test_review_submit_cli_sends_the_local_playbook_body(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from src.cli import reviews
    from src.cli.app import cli

    draft = tmp_path / "draft.md"
    draft.write_text(DOCUMENT, encoding="utf-8")
    body = tmp_path / "body.json"
    body.write_text(_body(), encoding="utf-8")
    captured = {}

    def execute(_ctx, command, params):
        captured.update(command=command, params=params)
        return {"success": True, "review_id": "rev-one", "revision": 1, "vault_path": "x.md"}

    monkeypatch.setattr(reviews, "_execute", execute)
    result = CliRunner().invoke(cli, [
        "review", "submit", "--project-id", "p", "--title", "Playbook V2: morning-report",
        "--file", str(draft), "--playbook-id", "morning-report",
        "--playbook-body", str(body), "--activate-on-approval", "--json",
    ])
    assert result.exit_code == 0, result.output
    assert captured["command"] == "review_submit"
    assert captured["params"] == {
        "content": DOCUMENT,
        "project_id": "p",
        "title": "Playbook V2: morning-report",
        "resolves": [],
        "playbook_id": "morning-report",
        "semantic_body": _body(),
        "activate_on_approval": True,
    }

    # A plain submission sends none of the playbook keys.
    CliRunner().invoke(cli, ["review", "submit", "--project-id", "p", "--kind", "spec",
                             "--title", "Spec", "--file", str(draft), "--json"])
    assert not {"playbook_id", "semantic_body", "activate_on_approval"} & set(captured["params"])
