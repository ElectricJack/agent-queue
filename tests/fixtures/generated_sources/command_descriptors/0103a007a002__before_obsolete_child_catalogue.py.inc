def _before_obsolete_child_catalogue():
    # Its frozen fallback order predates the parallel wait/restart changes.
    # Undo reviewed sibling values while retaining that exact ordering.
    return _before_parallel_catalogue()



