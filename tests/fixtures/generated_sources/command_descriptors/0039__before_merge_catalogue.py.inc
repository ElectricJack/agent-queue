def _before_merge_catalogue():
    return _revert_catalogue(_before_leaf_catalogue(), _merge_changes())


