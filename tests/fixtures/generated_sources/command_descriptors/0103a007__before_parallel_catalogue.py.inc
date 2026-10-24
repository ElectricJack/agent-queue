def _before_parallel_catalogue():
    catalogue = json.loads(render_command_catalogue())
    catalogue = _before_updater_catalogue(catalogue)
    catalogue = _before_pool_delete_catalogue(catalogue)
    catalogue = _before_retention_catalogue(catalogue)
    catalogue = _before_workspace_catalogue(catalogue)
    catalogue = _before_held_release_catalogue(catalogue)
    return catalogue


