def _held_ingestion_pipeline_transport_changes():
    return json.loads(
        (ROOT / "tests/fixtures/contracts/held-ingestion-pipeline-transport-changes.json").read_text()
    )


def _before_held_ingestion_pipeline_catalogue():
    return _revert_catalogue(
        json.loads(render_command_catalogue()), _held_ingestion_pipeline_transport_changes(),
    )


def test_held_ingestion_pipeline_only_promotes_three_reviewed_transports():
    evidence = _held_ingestion_pipeline_transport_changes()
    names = {"task_batch_review_record", "task_batch_release", "task_batch_retire"}
    for section in ("explicit", "fallback", "tool_categories"):
        assert set(evidence["catalogue_sections"][section]["entries"]) == names
    previous = json.dumps(_before_held_ingestion_pipeline_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == evidence["previous_cli_catalogue_sha256"]
