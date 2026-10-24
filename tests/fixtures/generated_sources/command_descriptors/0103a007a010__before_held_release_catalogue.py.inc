def _before_held_release_catalogue(catalogue=None):
    return _revert_held_release_catalogue(
        _before_held_ingestion_pipeline_catalogue() if catalogue is None else catalogue,
    )


