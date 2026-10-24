def _revert_hibernation_catalogue(catalogue):
    delta = _dev_hibernation_changes()["catalogue_delta"]
    previous = delta["previous_explicit"]
    catalogue["explicit"] = [
        previous.get(entry["name"], entry) for entry in catalogue["explicit"]
        if entry["name"] not in delta["added_explicit"]
    ]
    for key in ("fallback", "tool_categories"):
        for name in delta[f"added_{key}"]:
            catalogue[key].pop(name)
    return catalogue


