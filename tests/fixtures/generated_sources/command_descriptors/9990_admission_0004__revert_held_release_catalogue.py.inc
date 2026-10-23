def _revert_held_release_catalogue(catalogue):
    """Remove exactly the reviewed held-release delta, leaving every other entry in place."""
    delta = _held_release_changes()["catalogue_delta"]
    explicit = {entry["name"]: entry for entry in catalogue["explicit"]}
    assert set(delta["previous_explicit"]) == {"doctor"}
    assert set(delta["added_explicit"]) == {"spec_ingest_legacy_adopt"}
    for name, entry in delta["added_explicit"].items():
        assert explicit[name] == entry
    catalogue["explicit"] = [
        delta["previous_explicit"].get(entry["name"], entry)
        for entry in catalogue["explicit"] if entry["name"] not in delta["added_explicit"]
    ]
    assert set(delta["added_fallback"]) == {"task_batch_release", "task_batch_retire"}
    assert set(delta["added_tool_categories"]) == {"spec_ingest_legacy_adopt"}
    for key in ("fallback", "tool_categories"):
        for name, entry in delta[f"added_{key}"].items():
            assert catalogue[key].pop(name) == entry
    return catalogue


