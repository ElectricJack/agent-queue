def _dev_catalogue():
    return _revert_catalogue(_before_stuck_check_catalogue(), {
        "catalogue_sections": _completion_changes()["source_catalogue_sections"],
    })


