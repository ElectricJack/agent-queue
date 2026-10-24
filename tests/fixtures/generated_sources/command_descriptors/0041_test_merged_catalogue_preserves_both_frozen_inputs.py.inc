def test_merged_catalogue_preserves_both_frozen_inputs():
    evidence = _merge_changes()
    for key, expected in (("catalogue_sections", "previous_cli_catalogue_sha256"),
                          ("source_catalogue_sections", "source_cli_catalogue_sha256")):
        previous = _revert_catalogue(_before_leaf_catalogue(),
                                    {"catalogue_sections": evidence[key]})
        assert hashlib.sha256((json.dumps(previous, indent=2) + "\n").encode()).hexdigest() == (
            evidence[expected])


