def test_git_first_catalogue_only_updates_delivery_mode_description():
    evidence = _git_first_changes()
    change = evidence["catalogue_sections"]["explicit"]["entries"]["edit_project"]
    previous, current = change["previous"], change["current"]
    previous["input_schema"]["properties"]["hierarchical_integration_mode"]["description"] = (
        current["input_schema"]["properties"]["hierarchical_integration_mode"]["description"]
    )
    assert previous == current
    restored = _before_wait_requeue_catalogue()
    assert hashlib.sha256((json.dumps(restored, indent=2) + "\n").encode()).hexdigest() == (
        evidence["previous_cli_catalogue_sha256"]
    )



