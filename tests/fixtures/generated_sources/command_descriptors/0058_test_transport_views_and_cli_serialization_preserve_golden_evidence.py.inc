def test_transport_views_and_cli_serialization_preserve_golden_evidence():
    fixture = json.loads(GOLDEN.read_text())
    changes = _observation_changes()
    scripts = _script_changes()
    main = _main_changes()
    dev = _dev_changes()
    contracts = {**changes["contracts"], **scripts["contracts"]}
    snapshot = command_snapshot()
    for name, digest in fixture["transport_definitions"].items():
        if name in contracts:
            approved = contracts[name]
            assert approved["previous_transport_sha256"] == digest
            digest = approved["transport_sha256"]
        if name in main["transport_definitions"]:
            approved = main["transport_definitions"][name]
            assert approved["previous_sha256"] == digest
            digest = approved["sha256"]
        if name in dev["transport_definitions"]:
            approved = dev["transport_definitions"][name]
            assert approved["previous_sha256"] == digest
            digest = approved["sha256"]
        for recording in (
            _hibernation_changes(), _dispatch_changes(), _merge_changes(),
            _leaf_changes(), _completion_changes(), _restart_hold_changes(),
            _updater_changes(), _held_release_changes(), _workspace_changes(),
            _git_first_changes(), _obsolete_child_changes(),
        ):
            if name in recording["transport_definitions"]:
                approved = recording["transport_definitions"][name]
                assert approved["previous_sha256"] == digest
                digest = approved["sha256"]
        if name in _structure_changes()["transport_definitions"]:
            approved = _structure_changes()["transport_definitions"][name]
            assert _digest(approved["previous"]) == digest
            digest = _digest(approved["current"])
        if name == "task_batch_propose":
            settlement = _delivered_design_changes()
            assert _digest(settlement["previous_definition"]) == digest
            digest = _digest(settlement["definition"])
        assert _digest(snapshot.commands[name].tool_definition()) == digest, name
    catalogue = _revert_structure_catalogue(_before_restart_hold_catalogue())
    actual = hashlib.sha256((json.dumps(catalogue, indent=2) + "\n").encode()).hexdigest()
    assert changes["previous_cli_catalogue_sha256"] == fixture["cli_catalogue_sha256"]
    assert scripts["previous_cli_catalogue_sha256"] == changes["cli_catalogue_sha256"]
    assert main["previous_cli_catalogue_sha256"] == scripts["cli_catalogue_sha256"]
    assert dev["previous_cli_catalogue_sha256"] == main["cli_catalogue_sha256"]
    withheld = json.loads(WITHHELD_CHANGES.read_text())
    assert withheld["previous_cli_catalogue_sha256"] == dev["cli_catalogue_sha256"]
    epic = _epic_changes()
    assert epic["previous_cli_catalogue_sha256"] == withheld["cli_catalogue_sha256"]
    hibernation = _hibernation_changes()
    assert hibernation["previous_cli_catalogue_sha256"] == epic["cli_catalogue_sha256"]
    dispatch = _dispatch_changes()
    assert dispatch["previous_cli_catalogue_sha256"] == hibernation["cli_catalogue_sha256"]
    batch_fix = json.loads(BATCH_FIX_CHANGES.read_text())
    assert batch_fix["previous_cli_catalogue_sha256"] == dispatch["cli_catalogue_sha256"]
    merge = _merge_changes()
    assert merge["previous_cli_catalogue_sha256"] == batch_fix["cli_catalogue_sha256"]
    leaf = _leaf_changes()
    assert leaf["previous_cli_catalogue_sha256"] == merge["cli_catalogue_sha256"]
    completion = _completion_changes()
    assert completion["previous_cli_catalogue_sha256"] == leaf["cli_catalogue_sha256"]
    stuck_check = _stuck_check_changes()
    assert stuck_check["previous_cli_catalogue_sha256"] == completion["cli_catalogue_sha256"]
    pr_repair = _pr_repair_changes()
    assert pr_repair["previous_cli_catalogue_sha256"] == stuck_check["cli_catalogue_sha256"]
    wait_requeue = _wait_requeue_changes()
    assert wait_requeue["previous_cli_catalogue_sha256"] == pr_repair["cli_catalogue_sha256"]
    assert actual == wait_requeue["cli_catalogue_sha256"]
    previous = json.dumps(_before_batch_fix_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == dispatch["cli_catalogue_sha256"]
    previous = json.dumps(_before_dispatch_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == hibernation["cli_catalogue_sha256"]
    previous = json.dumps(_before_hibernation_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == epic["cli_catalogue_sha256"]
    definitions = {d["name"]: d for d in effective_tool_definitions()}
    for name, definition in _discover_all_commands().items():
        assert definition == snapshot.commands[name].tool_definition()
        if name in definitions:
            assert definition == definitions[name]


