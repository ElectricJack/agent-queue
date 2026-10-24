def _before_workspace_catalogue(catalogue=None):
    return _revert_catalogue(
        json.loads(render_command_catalogue()) if catalogue is None else catalogue,
        _workspace_changes(),
    )


