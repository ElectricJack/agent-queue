def _before_workspace_catalogue(catalogue=None):
    return _revert_catalogue(
        _before_held_ingestion_pipeline_catalogue() if catalogue is None else catalogue,
        _workspace_changes(),
    )


