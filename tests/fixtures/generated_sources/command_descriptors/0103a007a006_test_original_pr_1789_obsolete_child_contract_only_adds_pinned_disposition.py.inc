def test_original_pr_1789_obsolete_child_contract_only_adds_pinned_disposition():
    changes = _obsolete_child_changes()
    old = _dev_changes()["contracts"]["integration_redrive_child"]
    assert changes["contracts"]["integration_redrive_child"]["previous_fingerprint"] == (
        old["fingerprint"]
    )
    old = old["execution"]
    optional = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
    old["args_schema"]["properties"].update(
        dispose_obsolete={"default": False, "type": "boolean"},
        superseded_by=optional, expected_parent_head_sha=optional,
    )
    position = old["receipt_projection"].index("tree_sha")
    for field in ("parent_head_sha", "merge_head_sha", "superseded_by"):
        old["result_schema"]["properties"][field] = optional
        old["receipt_projection"].insert(position, field)
        position += 1
    assert changes["contracts"]["integration_redrive_child"]["execution"] == old
    previous = json.dumps(_before_obsolete_child_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == (
        changes["previous_cli_catalogue_sha256"]
    ) == _pr_repair_changes()["cli_catalogue_sha256"]



