def _before_held_release_catalogue(catalogue=None):
    return _revert_held_release_catalogue(
        json.loads(render_command_catalogue()) if catalogue is None else catalogue,
    )


