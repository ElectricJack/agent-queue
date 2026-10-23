def test_inventory_coverage_is_derived_with_exact_denials():
    snapshot = command_snapshot()
    discovered = {h.name for h in discover_handler_inventory(CommandHandler)}
    assert snapshot.handler_names == discovered
    assert discovered - PLAYBOOK_DENIALS.keys() <= CONTRACTS.names()
    assert not (PLAYBOOK_DENIALS.keys() & CONTRACTS.names())
    assert {n for n, d in snapshot.commands.items() if d.denial_reason} == set(PLAYBOOK_DENIALS)
    report = {r["name"]: r for r in snapshot.schema_quality_report()}
    assert report.keys() == snapshot.commands.keys()
    assert any(r["quality"] == "opaque" for r in report.values())


