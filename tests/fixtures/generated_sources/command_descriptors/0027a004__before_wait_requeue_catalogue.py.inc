def _before_wait_requeue_catalogue():
    return _revert_catalogue(
        _revert_structure_catalogue(_before_restart_hold_catalogue()),
        _wait_requeue_changes(),
    )


