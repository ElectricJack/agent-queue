def _before_completion_catalogue():
    return _revert_catalogue(_before_stuck_check_catalogue(), _completion_changes())


