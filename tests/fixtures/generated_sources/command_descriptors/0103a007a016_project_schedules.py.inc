

def _project_schedule_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/project-schedule-contract-changes.json").read_text()
    )


def _before_project_schedules_catalogue():
    return _revert_catalogue(json.loads(render_command_catalogue()), _project_schedule_changes())


def test_project_schedules_only_add_scoped_notification_commands():
    evidence = _project_schedule_changes()
    names = {"schedule_register", "schedule_get", "schedule_list", "schedule_cancel"}
    assert set(evidence["contracts"]) == names
    sections = evidence["catalogue_sections"]
    assert set(sections["explicit"]["entries"]) == names
    assert set(sections["tool_categories"]["entries"]) == names
    assert set(sections["fallback"]["entries"]) == {"reconcile_project_schedules"}
    assert set(sections["categories"]["entries"]) == {"schedule"}
    for section in sections.values():
        assert all(entry["previous"] is None for entry in section["entries"].values())
    for name, approved in evidence["contracts"].items():
        contract = CONTRACTS.require(name).contract
        assert contract.fingerprint() == approved["fingerprint"]
        assert canonical_execution_document(contract.execution) == approved["execution"]
    previous = json.dumps(_before_project_schedules_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
