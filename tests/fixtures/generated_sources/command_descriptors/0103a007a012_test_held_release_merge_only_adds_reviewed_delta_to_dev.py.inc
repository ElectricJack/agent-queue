def test_held_release_merge_only_adds_reviewed_delta_to_dev():
    held = _held_release_changes()
    merge = _held_release_merge_changes()
    assert merge["source_cli_catalogue_sha256"] == held["cli_catalogue_sha256"]
    assert merge["previous_cli_catalogue_sha256"] == _pr_repair_changes()["cli_catalogue_sha256"]
    # Removing exactly the held-release delta recovers dev's frozen catalogue bytes.
    previous = json.dumps(_before_wait_requeue_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == merge["previous_cli_catalogue_sha256"]
    snapshot = command_snapshot()
    # Reverse the separately reviewed transport promotion before checking this historical pin.
    catalogue = _before_held_ingestion_pipeline_catalogue()
    for name, approved in held["added_contracts"].items():
        transport = catalogue["fallback"].get(name, snapshot.commands[name].tool_definition())
        assert _digest(transport) == approved["transport_sha256"]
        if "execution" in approved:
            contract = CONTRACTS.require(name).contract
            assert canonical_execution_document(contract.execution) == approved["execution"]
            assert contract.fingerprint() == approved["fingerprint"]


