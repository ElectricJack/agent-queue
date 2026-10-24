def test_legacy_playbook_fixtures_are_unchanged():
    changes = _observation_changes()["playbook_fixtures"]
    assert set(changes) == {
        "tests/fixtures/playbooks/v2/stuck-task-watch/artifact.json",
        "tests/fixtures/playbooks/v2/review-pipeline.artifact.json",
        "tests/fixtures/playbooks/v2/review-pipeline.v6.artifact.json",
    }
    changes = {**changes, **_script_changes()["playbook_fixtures"]}
    main = _main_changes()["playbook_fixtures"]
    watch = _watch_changes()["playbook_fixtures"]
    ingest = json.loads(INGEST_CHANGES.read_text())["playbook_fixtures"]
    hibernation = _hibernation_changes()["playbook_fixtures"]
    probe_port = json.loads((
        ROOT / "tests/fixtures/contracts/provider-usage-probe-port-changes.json"
    ).read_text())["playbook_fixtures"]
    assert set(probe_port) == {"tests/fixtures/playbooks/v2/provider-usage-probe/artifact.json"}
    object_loop_port = json.loads(OBJECT_LOOP_PORT_CHANGES.read_text())["playbook_fixtures"]
    assert set(object_loop_port) == {"tests/fixtures/playbooks/v2/object-loop/artifact.json"}
    object_loop = json.loads(OBJECT_LOOP_CHANGES.read_text())["playbook_fixtures"]
    assert set(ingest) == {"tests/fixtures/playbooks/v2/default-pipeline/artifact.json"}
    assert set(object_loop) == {
        "tests/fixtures/playbooks/v2/object-loop/artifact.json",
        "tests/fixtures/playbooks/v2/object-loop/traces.json",
    }
    promotion_request = json.loads(PROMOTION_REQUEST_PORT.read_text())["playbook_fixtures"]
    assert set(promotion_request) == {"tests/fixtures/playbooks/v2/promotion-request/artifact.json"}
    routing = json.loads(ROUTING_CHANGES.read_text())["playbook_fixtures"]
    assert set(ingest) == {"tests/fixtures/playbooks/v2/default-pipeline/artifact.json"}
    assert set(routing) == {"tests/fixtures/playbooks/v2/default-assignment-routing/artifact.json"}
    held_pipeline = _held_ingestion_pipeline_transport_changes()["playbook_fixtures"]
    pipeline = json.loads(PIPELINE_CHANGES.read_text())["playbook_fixtures"]
    assert set(pipeline) == {"tests/fixtures/playbooks/v2/default-pipeline/artifact.json"}
    failover = json.loads(FAILOVER_CHANGES.read_text())["playbook_fixtures"]
    assert set(failover) == {"tests/fixtures/playbooks/v2/provider-failover/artifact.json"}
    triage = json.loads(TRIAGE_CHANGES.read_text())["playbook_fixtures"]
    assert set(triage) == {"tests/fixtures/playbooks/v2/supervisor-failure-triage/artifact.json"}
    morning = json.loads(MORNING_CHANGES.read_text())["playbook_fixtures"]
    assert set(morning) == {"tests/fixtures/playbooks/v2/morning-report/artifact.json"}
    hourly = json.loads(
        (ROOT / "tests/fixtures/contracts/discord-report-contract-changes.json").read_text()
    )["playbook_fixtures"]
    assert set(hourly) == {"tests/fixtures/playbooks/v2/supervisor-hourly-report/artifact.json"}
    for path, digest in json.loads(GOLDEN.read_text())["playbook_fixtures"].items():
        if path in changes:
            assert changes[path]["previous_sha256"] == digest
            digest = changes[path]["sha256"]
        if path in main:
            assert main[path]["previous_sha256"] == digest
            digest = main[path]["sha256"]
            path = main[path]["path"]
        if path in watch:
            assert watch[path]["previous_sha256"] == digest
            historical = ROOT / watch[path]["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert watch[path]["sha256"] != digest
            digest = watch[path]["sha256"]
        if path in ingest:
            assert ingest[path]["previous_sha256"] == digest
            assert ingest[path]["sha256"] != digest
            digest = ingest[path]["sha256"]
        if path in hibernation:
            assert hibernation[path]["previous_sha256"] == digest
            assert hibernation[path]["sha256"] != digest
            digest = hibernation[path]["sha256"]
        if path in probe_port:
            assert probe_port[path]["previous_sha256"] == digest
            historical = ROOT / probe_port[path]["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert probe_port[path]["sha256"] != digest
            digest = probe_port[path]["sha256"]
        if path in object_loop_port:
            change = object_loop_port[path]
            assert change["previous_sha256"] == digest
            historical = ROOT / change["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            ported = ROOT / "tests/fixtures/playbooks/object-loop-port/ported.artifact.json"
            assert hashlib.sha256(ported.read_bytes()).hexdigest() == change["sha256"]
            # The two reviewed recordings derive independently from this baseline.
            # Keep the authoring port intact while the filter recording is current.
        if path in object_loop:
            assert object_loop[path]["previous_sha256"] == digest
            assert object_loop[path]["sha256"] != digest
            digest = object_loop[path]["sha256"]
        if path in failover:
            assert failover[path]["previous_sha256"] == digest
            historical = ROOT / failover[path]["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert failover[path]["sha256"] != digest
            digest = failover[path]["sha256"]
        if path in triage:
            assert triage[path]["previous_sha256"] == digest
            historical = ROOT / triage[path]["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert triage[path]["sha256"] != digest
            digest = triage[path]["sha256"]
        if path in morning:
            assert morning[path]["previous_sha256"] == digest
            historical = ROOT / morning[path]["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert morning[path]["sha256"] != digest
            digest = morning[path]["sha256"]
        if path in hourly:
            assert hourly[path]["previous_sha256"] == digest
            assert hourly[path]["sha256"] != digest
            digest = hourly[path]["sha256"]
        if path in promotion_request:
            change = promotion_request[path]
            assert change["previous_sha256"] == digest
            historical = ROOT / change["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert change["sha256"] != digest
            digest = change["sha256"]
        if path in routing:
            assert routing[path]["previous_sha256"] == digest
            historical = ROOT / routing[path]["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert routing[path]["sha256"] != digest
            digest = routing[path]["sha256"]
        if path in pipeline:
            assert pipeline[path]["previous_sha256"] == digest
            historical = ROOT / pipeline[path]["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert pipeline[path]["sha256"] != digest
            digest = pipeline[path]["sha256"]
        if path in held_pipeline:
            change = held_pipeline[path]
            assert change["previous_sha256"] == digest
            historical = ROOT / change["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert change["sha256"] != digest
            digest = change["sha256"]
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path


