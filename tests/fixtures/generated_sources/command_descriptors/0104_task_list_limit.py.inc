

def _task_list_limit_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/task-list-limit-contract-changes.json").read_text()
    )


def _before_task_list_limit_catalogue(catalogue=None):
    # Compose independent catalogue deltas while preserving their frozen hashes.
    if catalogue is None:
        catalogue = _before_wait_source_redispatch_catalogue()
    evidence = _task_list_limit_changes()["transport_definitions"]["list_tasks"]
    [definition] = [row for row in catalogue["explicit"] if row["name"] == "list_tasks"]
    assert definition == evidence["definition"]
    definition.clear()
    definition.update(evidence["previous_definition"])
    return catalogue


def test_task_list_limit_only_adds_optional_positive_limit():
    evidence = _task_list_limit_changes()
    assert set(evidence["contracts"]) == set(evidence["transport_definitions"]) == {"list_tasks"}
    previous = _observation_changes()["contracts"]["list_tasks"]
    approved = evidence["contracts"]["list_tasks"]
    assert approved["previous_fingerprint"] == previous["fingerprint"]
    contract = CONTRACTS.require("list_tasks").contract
    assert contract.fingerprint() == approved["fingerprint"] != previous["fingerprint"]
    assert canonical_execution_document(contract.execution) == approved["execution"]
    execution = json.loads(json.dumps(approved["execution"]))
    limit = execution["args_schema"]["properties"].pop("limit")
    assert limit == {
        "anyOf": [{"exclusiveMinimum": 0, "type": "integer"}, {"type": "null"}],
        "default": None,
    }
    assert execution == previous["execution"]
    transport = evidence["transport_definitions"]["list_tasks"]
    definition = json.loads(json.dumps(transport["definition"]))
    limit = definition["input_schema"]["properties"].pop("limit")
    assert limit["type"] == "integer" and limit["minimum"] == 1
    assert definition == transport["previous_definition"]
    assert _digest(transport["definition"]) == transport["sha256"]
    assert _digest(transport["previous_definition"]) == transport["previous_sha256"]
    assert hashlib.sha256(
        (json.dumps(_before_wait_source_redispatch_catalogue(), indent=2) + "\n").encode()
    ).hexdigest() == (
        evidence["cli_catalogue_sha256"]
    )
    previous = json.dumps(_before_task_list_limit_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]


def test_task_list_limit_playbooks_only_refresh_command_pins():
    evidence = _task_list_limit_changes()
    assert set(evidence["playbook_fixtures"]) == {
        "tests/fixtures/playbooks/v2/review-pipeline.artifact.json",
        "tests/fixtures/playbooks/v2/review-pipeline.v6.artifact.json",
        "tests/fixtures/playbooks/v2/stuck-task-watch/artifact.json",
        "tests/fixtures/playbooks/v2/stuck-task-watch-reminder/artifact.json",
        "tests/fixtures/playbooks/v2/stuck-task-watch-reminder/artifact.sha256",
        "tests/fixtures/playbooks/v2/stuck-task-watch-reminder/manifest.md",
    }
    contract = evidence["contracts"]["list_tasks"]
    for path, change in evidence["playbook_fixtures"].items():
        old_bytes = (ROOT / change["preserved_previous_path"]).read_bytes()
        new_bytes = (ROOT / path).read_bytes()
        assert hashlib.sha256(old_bytes).hexdigest() == change["previous_sha256"]
        assert hashlib.sha256(new_bytes).hexdigest() == change["sha256"]
        if not path.endswith(".json"):
            continue
        old, new = json.loads(old_bytes), json.loads(new_bytes)
        assert old["compiled_against"]["commands"].pop("list_tasks") == (
            contract["previous_fingerprint"]
        )
        assert new["compiled_against"]["commands"].pop("list_tasks") == contract["fingerprint"]
        old.pop("compiled_at")
        new.pop("compiled_at")
        assert new == old, path
