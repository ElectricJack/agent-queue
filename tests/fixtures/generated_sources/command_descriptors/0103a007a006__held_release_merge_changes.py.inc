def _held_release_merge_changes():
    changes = json.loads(HELD_RELEASE_MERGE_CHANGES.read_text())
    assert changes["source_base"] == "94e69a6a60eaa640ee3cae13dd21edffb1ffc2c7"
    assert changes["merged_source"] == "ddc3e6e6d9ba2ffa9af6fb1da041fa55115d2c5a"
    return changes


