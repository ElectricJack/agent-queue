def _before_hibernation_catalogue():
    return _revert_catalogue(_before_dispatch_catalogue(), _hibernation_changes())


