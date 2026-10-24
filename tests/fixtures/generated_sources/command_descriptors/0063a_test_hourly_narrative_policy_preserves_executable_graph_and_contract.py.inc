def test_hourly_narrative_policy_preserves_executable_graph_and_contract():
    evidence = json.loads(
        (ROOT / "tests/fixtures/contracts/hourly-narrative-policy-changes.json").read_text()
    )
    path = "tests/fixtures/playbooks/v2/supervisor-hourly-report/artifact.json"
    change = evidence["playbook_fixtures"][path]
    old = json.loads((ROOT / change["preserved_previous_path"]).read_bytes())
    new = json.loads((ROOT / path).read_bytes())
    for artifact in (old, new):
        for metadata in ("compiled_at", "source_hash"):
            artifact.pop(metadata)
        for node in [*artifact["rules"], *artifact["steps"].values()]:
            node.pop("source")
    assert new == old


