def _before_leaf_catalogue():
    return _revert_catalogue(_before_completion_catalogue(), _leaf_changes())


