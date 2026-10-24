def test_original_pr_1842_legacy_playbook_fixtures_are_unchanged():
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
    continuous = json.loads(CONTINUOUS_CHANGES.read_text())["playbook_fixtures"]
    assert set(continuous) == {"tests/fixtures/playbooks/v2/promotion-continuous/artifact.json"}
    promotion_request = json.loads(PROMOTION_REQUEST_PORT.read_text())["playbook_fixtures"]
    assert set(promotion_request) == {"tests/fixtures/playbooks/v2/promotion-request/artifact.json"}
    routing = json.loads(ROUTING_CHANGES.read_text())["playbook_fixtures"]
    assert set(ingest) == {"tests/fixtures/playbooks/v2/default-pipeline/artifact.json"}
    assert set(routing) == {"tests/fixtures/playbooks/v2/default-assignment-routing/artifact.json"}
    held_pipeline = _held_ingestion_pipeline_transport_changes()["playbook_fixtures"]
    pipeline = json.loads(PIPELINE_CHANGES.read_text())["playbook_fixtures"]
    assert set(pipeline) == {"tests/fixtures/playbooks/v2/default-pipeline/artifact.json"}
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
        if path in continuous:
            assert continuous[path]["previous_sha256"] == digest
            historical = ROOT / continuous[path]["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            assert continuous[path]["sha256"] != digest
            digest = continuous[path]["sha256"]
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


