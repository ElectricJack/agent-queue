def _main_changes():
    changes = json.loads(MAIN_CHANGES.read_text())
    assert changes["source_base"] == "c29fcd7804cef4e96bc45f3a3502ae4e8d3e70cb"
    assert set(changes["contracts"]) == {
        "gate_create", "gate_resolve", "integration_backmerge_source",
        "integration_promotion_notes_input", "integration_promotion_policy_input",
        "integration_release_owner", "promote_approve", "promote_cancel", "promote_hotfix",
        "promote_list", "promote_prepare", "promote_repair", "promote_request", "promote_status",
    }
    assert set(changes["transport_definitions"]) == {
        "add_workspace", "archive_task", "escalation_apply_reply", "gate_create", "gate_resolve",
        "integration_promotion_policy_input", "integration_release_owner", "list_workspaces",
        "message_send", "promote_request", "resume_playbook", "resume_task", "review_comment",
        "review_decide", "task_batch_propose", "task_batch_update", "task_close", "task_recover",
    }
    active = {
        "ci-main-sentinel/artifact.json", "default-assignment-routing/artifact.json",
        "default-pipeline/artifact.json", "github-issue-triage/artifact.json",
        "morning-report/artifact.json", "object-loop/artifact.json",
        "promotion-continuous/artifact.json", "promotion-request/artifact.json",
        "provider-failover/artifact.json", "provider-usage-probe/artifact.json",
        "review-pipeline.artifact.json", "review-pipeline.receipts.json",
        "review-pipeline.v6.artifact.json", "stuck-task-watch/artifact.json",
        "supervisor-failure-triage/artifact.json",
    }
    retired = {f"{bundle}/{name}.json" for bundle in (
        "agent-queue-parent-integration", "agent-queue-root-train", "parent-integration", "root-train",
    ) for name in ("artifact", "diagnostics")}
    assert set(changes["playbook_fixtures"]) == {
        "tests/fixtures/playbooks/v2/" + path for path in active | retired
    }
    for path, evidence in changes["playbook_fixtures"].items():
        suffix = path.removeprefix("tests/fixtures/playbooks/v2/")
        expected = path.replace("/v2/", "/historical-v2/") if suffix in retired else path
        assert evidence["path"] == expected
    return changes


