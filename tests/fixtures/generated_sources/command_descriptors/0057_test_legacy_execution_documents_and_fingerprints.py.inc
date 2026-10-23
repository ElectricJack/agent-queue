def test_legacy_execution_documents_and_fingerprints():
    fixture = json.loads(GOLDEN.read_text())
    changes = {**_observation_changes()["contracts"], **_script_changes()["contracts"],
               **_main_changes()["contracts"], **_dev_changes()["contracts"]}
    for name, old in fixture["legacy_contracts"].items():
        expected = changes.get(name, old)
        if name in changes:
            assert expected["previous_fingerprint"] == old["fingerprint"]
            assert expected["fingerprint"] != old["fingerprint"]
        for recording in (_hibernation_changes(), _dispatch_changes(), _merge_changes(),
                          _leaf_changes(), _completion_changes()):
            later = recording["contracts"].get(name)
            if later:
                assert later["previous_fingerprint"] == expected["fingerprint"]
                assert later["fingerprint"] != expected["fingerprint"]
                expected = later
        contract = CONTRACTS.require(name).contract
        assert canonical_execution_document(contract.execution) == expected["execution"], name
        assert contract.fingerprint() == expected["fingerprint"], name
    assert set(fixture["legacy_contracts"]) - command_snapshot().handler_names == {
        "count_project_memory_files", "read_project_memory_file", "git_diff",
        "memory_save", "memory_search",
    }
    descriptors = command_snapshot().commands
    for name in ("count_project_memory_files", "read_project_memory_file"):
        assert descriptors[name].origin == "helper" and descriptors[name].available
    for name in ("git_diff", "memory_save", "memory_search"):
        assert descriptors[name].origin == "plugin" and descriptors[name].plugin


