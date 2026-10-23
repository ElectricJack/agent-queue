def test_generated_inventory_does_not_suppress_legacy_scope_injection():
    from src.api.scope import _forbids_project_id

    generated = CONTRACTS.names() - CONTRACTS.typed_contracts().keys()
    assert generated
    assert not {name for name in generated if _forbids_project_id(name)}


