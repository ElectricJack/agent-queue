def _before_parallel_catalogue():
    catalogue = _before_held_ingestion_pipeline_catalogue()
    catalogue = _before_updater_catalogue(catalogue)
    catalogue = _before_pool_delete_catalogue(catalogue)
    catalogue = _before_retention_catalogue(catalogue)
    catalogue = _before_held_release_catalogue(catalogue)
    # These sibling PRs changed existing entries on different frozen bases.
    # Undo their reviewed values before applying workspace's complete ordering.
    for changes in (_git_first_changes(), _obsolete_child_changes()):
        for section, evidence in changes["catalogue_sections"].items():
            if not evidence["entries"]:
                continue
            entries = ({row["name"]: row for row in catalogue[section]}
                       if evidence["list"] else catalogue[section])
            for name, change in evidence["entries"].items():
                assert entries[name] == change["current"]
                assert change["previous"] is not None
                entries[name].clear()
                entries[name].update(change["previous"])
    catalogue = _before_workspace_catalogue(catalogue)
    return catalogue


