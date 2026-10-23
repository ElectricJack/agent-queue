def _before_dispatch_catalogue():
    return _revert_catalogue(_before_batch_fix_catalogue(), _dispatch_changes())


