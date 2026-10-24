def _delivered_design_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/delivered-design-settlement-contract-changes.json").read_text()
    )


def _before_delivered_design_catalogue():
    catalogue = json.loads(render_command_catalogue())
    evidence = _delivered_design_changes()
    [definition] = [row for row in catalogue["explicit"] if row["name"] == "task_batch_propose"]
    assert definition == evidence["definition"]
    definition.clear()
    definition.update(evidence["previous_definition"])
    return catalogue


def test_delivered_design_catalogue_only_adds_explicit_settlement():
    evidence = _delivered_design_changes()
    previous = json.dumps(_before_delivered_design_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
    current = render_command_catalogue()
    assert hashlib.sha256(current.encode()).hexdigest() == evidence["cli_catalogue_sha256"]
    definition = json.loads(json.dumps(evidence["definition"]))
    settlement = definition["input_schema"]["properties"].pop("no_op")
    assert settlement["required"] == ["reason", "delivered_evidence"]
    assert settlement["properties"]["delivered_evidence"]["minItems"] == 1
    definition["description"] = evidence["previous_definition"]["description"]
    assert definition == evidence["previous_definition"]


