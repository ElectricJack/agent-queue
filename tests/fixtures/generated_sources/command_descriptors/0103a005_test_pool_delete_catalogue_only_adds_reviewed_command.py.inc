def test_pool_delete_catalogue_only_adds_reviewed_command():
    evidence = _pool_delete_changes()
    catalogue = _before_wait_requeue_catalogue()
    previous = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == (
        evidence["previous_cli_catalogue_sha256"]
    )


