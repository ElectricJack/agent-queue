def test_watch_reminder_additions_match_explicit_pins():
    for path, digest in _watch_changes()["new_playbook_fixtures"].items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == digest, path


