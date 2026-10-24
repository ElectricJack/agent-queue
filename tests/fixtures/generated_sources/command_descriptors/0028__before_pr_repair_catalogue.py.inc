def _before_pr_repair_catalogue():
    evidence = _pr_repair_changes()
    catalogue = _before_wait_requeue_catalogue()
    assert catalogue["fallback"].pop(evidence["command"]) == evidence["transport_definition"]
    return catalogue


