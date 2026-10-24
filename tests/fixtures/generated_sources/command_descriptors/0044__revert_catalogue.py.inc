def _revert_catalogue(catalogue, changes):
    for section, evidence in changes["catalogue_sections"].items():
        current = catalogue[section]
        entries = (dict(current) if section == "internal" else
                   {row["name"]: row for row in current} if evidence["list"] else current)
        for name, change in evidence["entries"].items():
            assert entries.get(name) == change["current"], (section, name)
            if change["previous"] is None:
                entries.pop(name)
            else:
                entries[name] = change["previous"]
        previous = {name: entries[name] for name in evidence["previous_order"]}
        assert previous.keys() == entries.keys()
        catalogue[section] = (list(map(list, previous.items())) if section == "internal" else
                              list(previous.values()) if evidence["list"] else previous)
    return catalogue


