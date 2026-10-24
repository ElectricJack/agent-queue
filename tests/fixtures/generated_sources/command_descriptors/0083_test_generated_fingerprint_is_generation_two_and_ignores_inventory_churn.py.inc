def test_generated_fingerprint_is_generation_two_and_ignores_inventory_churn():
    descriptor = _snapshot(handlers=discover_handler_inventory(NewHandler)).commands["newly_discovered"]
    first = generated_registration(descriptor).contract
    assert canonical_execution_document(first.execution)["schema_generation"] == 2
    unavailable = generated_registration(replace(descriptor, available=False)).contract
    assert first.fingerprint() == unavailable.fingerprint()
    changed = generated_registration(replace(descriptor, sensitivity="audited_public")).contract
    assert changed.fingerprint() != first.fingerprint()


