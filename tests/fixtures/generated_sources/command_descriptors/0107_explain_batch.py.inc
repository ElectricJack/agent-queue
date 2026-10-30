

def _explain_batch_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/explain-batch-contract-changes.json").read_text()
    )


def _before_explain_batch_catalogue(catalogue=None):
    catalogue = json.loads(render_command_catalogue()) if catalogue is None else catalogue
    evidence = _explain_batch_changes()
    definition = catalogue["fallback"][evidence["command"]]
    assert definition == evidence["transport_definition"]
    catalogue["fallback"][evidence["command"]] = evidence["previous_transport_definition"]
    return catalogue


def test_explain_batch_only_adds_optional_task_ids():
    evidence = _explain_batch_changes()
    rendered = render_command_catalogue()
    assert hashlib.sha256(rendered.encode()).hexdigest() == evidence["cli_catalogue_sha256"]
    digests = evidence["transport_definitions"][evidence["command"]]
    assert _digest(evidence["transport_definition"]) == digests["sha256"]
    assert _digest(evidence["previous_transport_definition"]) == digests["previous_sha256"]
    current = json.loads(json.dumps(evidence["transport_definition"]))
    batch = current["input_schema"]["properties"].pop("task_ids")
    assert (batch["type"], batch["items"], batch["minItems"], batch["maxItems"]) == (
        "array", {"type": "string"}, 1, 100)
    current["input_schema"]["required"] = ["task_id"]
    assert current == evidence["previous_transport_definition"]
    previous = json.dumps(_before_explain_batch_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == (
        evidence["previous_cli_catalogue_sha256"]
    )
