def test_retention_original_catalogue_evidence():
    evidence = _retention_changes()
    catalogue = _before_wait_requeue_catalogue()
    template = json.loads(render_command_catalogue())
    explicit = {row["name"]: row for row in catalogue["explicit"]}
    explicit[evidence["command"]] = evidence["explicit_entry"]
    catalogue["explicit"] = [explicit[row["name"]] for row in template["explicit"]
                             if row["name"] in explicit]
    categories = {**catalogue["tool_categories"], evidence["command"]: evidence["tool_category"]}
    catalogue["tool_categories"] = {name: categories[name] for name in template["tool_categories"]
                                    if name in categories}
    original = json.dumps(catalogue, indent=2) + "\n"
    assert hashlib.sha256(original.encode()).hexdigest() == evidence["cli_catalogue_sha256"]

