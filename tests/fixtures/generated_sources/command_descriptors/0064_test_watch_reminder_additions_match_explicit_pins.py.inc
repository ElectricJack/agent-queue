def test_watch_reminder_additions_match_explicit_pins():
    task_list_limit = _task_list_limit_changes()["playbook_fixtures"]
    for path, digest in _watch_changes()["new_playbook_fixtures"].items():
        if path in task_list_limit:
            change = task_list_limit[path]
            assert change["previous_sha256"] == digest
            historical = ROOT / change["preserved_previous_path"]
            assert hashlib.sha256(historical.read_bytes()).hexdigest() == digest
            digest = change["sha256"]
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path


