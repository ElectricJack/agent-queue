def test_original_pr_1789_obsolete_child_contract_only_adds_pinned_disposition():
    # Obsolete-child and PR-repair snapshots have distinct frozen ordering;
    # preserve each recorded digest rather than equating their source bases.
    test_obsolete_child_contract_only_adds_pinned_disposition()



