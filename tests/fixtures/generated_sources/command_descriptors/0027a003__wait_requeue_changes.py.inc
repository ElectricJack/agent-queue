def _wait_requeue_changes():
    return json.loads(WAIT_REQUEUE_CHANGES.read_text())


