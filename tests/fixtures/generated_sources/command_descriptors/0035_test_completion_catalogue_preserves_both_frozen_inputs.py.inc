def test_completion_catalogue_preserves_both_frozen_inputs():
    evidence = _completion_changes()
    for catalogue, expected in (
        (_before_completion_catalogue(), "previous_cli_catalogue_sha256"),
        (_dev_catalogue(), "source_cli_catalogue_sha256"),
    ):
        assert hashlib.sha256((json.dumps(catalogue, indent=2) + "\n").encode()).hexdigest() == (
            evidence[expected]
        )
    assert evidence["source_cli_catalogue_sha256"] == _target_repair_changes()[
        "cli_catalogue_sha256"
    ]



