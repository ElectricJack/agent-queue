def _before_obsolete_child_catalogue():
    return _revert_catalogue(json.loads(render_command_catalogue()), _obsolete_child_changes())



