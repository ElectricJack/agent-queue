

def _publication_hold_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/wait-publication-hold-contract-changes.json").read_text()
    )


def _before_publication_hold_catalogue(catalogue=None):
    evidence = _publication_hold_changes()
    if catalogue is None:
        catalogue = _before_retained_holder_repair_catalogue(
            json.loads(render_command_catalogue())
        )
    [definition] = [row for row in catalogue["explicit"] if row["name"] == evidence["command"]]
    assert definition == evidence["definition"]
    definition.clear()
    definition.update(evidence["previous_definition"])
    return catalogue


def test_publication_hold_only_adds_fenced_continuation_retirement():
    evidence = _publication_hold_changes()
    contract = CONTRACTS.require(evidence["command"]).contract
    assert contract.fingerprint() == evidence["contract_fingerprint"]
    assert canonical_execution_document(contract.execution) == evidence["execution"]
    for key, fingerprint in (("previous_execution", "previous_contract_fingerprint"),
                             ("execution", "contract_fingerprint")):
        blob = json.dumps(evidence[key], sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode()
        assert "sha256:" + hashlib.sha256(blob).hexdigest() == evidence[fingerprint]
    assert evidence["previous_contract_fingerprint"] == _wait_requeue_changes()["contract_fingerprint"]
    previous = evidence["previous_execution"]
    current = json.loads(json.dumps(evidence["execution"]))
    schema = current["args_schema"]
    assert schema["properties"]["decision"]["enum"].pop() == "retire"
    assert schema["properties"]["wait_id"] == {
        "anyOf": [{"minLength": 1, "type": "string"}, {"type": "null"}], "default": None,
    }
    schema["properties"]["wait_id"] = previous["args_schema"]["properties"]["wait_id"]
    schema["required"] = previous["args_schema"]["required"]
    assert current == previous
    before = json.dumps(_before_publication_hold_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(before.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
    rendered = json.dumps(
        _before_retained_holder_repair_catalogue(json.loads(render_command_catalogue())), indent=2
    ) + "\n"
    assert hashlib.sha256(rendered.encode()).hexdigest() == evidence["cli_catalogue_sha256"]
