def test_wait_requeue_catalogue_only_changes_reviewed_command():
    evidence = _wait_requeue_changes()
    assert _publication_hold_changes()["previous_contract_fingerprint"] == (
        evidence["contract_fingerprint"]
    )
    assert {name for section in evidence["catalogue_sections"].values()
            for name in section["entries"]} == {evidence["command"]}
    previous = json.dumps(_before_wait_requeue_catalogue(), indent=2) + "\n"
    assert hashlib.sha256(previous.encode()).hexdigest() == (
        evidence["previous_cli_catalogue_sha256"]
    )
    assert evidence["previous_cli_catalogue_sha256"] == _pr_repair_changes()[
        "cli_catalogue_sha256"
    ]


