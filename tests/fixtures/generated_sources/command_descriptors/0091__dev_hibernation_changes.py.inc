def _dev_hibernation_changes():
    changes = json.loads(DEV_HIBERNATION_CHANGES.read_text())
    assert changes["source_base"] == "4385c73158425a08114164893b623cf6b65718e8"
    assert set(changes["contracts"]) == {
        "job_cancel", "job_get", "job_submit", "message_wait",
        "wait_cancel", "wait_get", "wait_list", "wait_register",
    }
    assert set(changes["transport_definitions"]) == {
        "edit_task", "wait_cancel", "wait_get", "wait_list", "wait_register",
    }
    return changes


