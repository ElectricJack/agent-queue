from unittest.mock import Mock

from click.testing import CliRunner

from src.cli.sessions import session


def test_logs_passes_attempt_and_renders_recorded_entries(monkeypatch):
    call = Mock(return_value={"success": True, "source": "transcript",
                             "entries": [{"type": "assistant", "text": "Saved finding"}]})
    monkeypatch.setattr("src.cli.sessions._call", call)
    result = CliRunner().invoke(session, ["logs", "worker", "--attempt", "attempt-1"])
    assert result.exit_code == 0, result.output
    assert call.call_args.args[2]["attempt_id"] == "attempt-1"
    assert "Saved finding" in result.output


def test_logs_displays_missing_recording_explanation(monkeypatch):
    monkeypatch.setattr("src.cli.sessions._call", Mock(return_value={
        "success": True, "source": "unavailable", "note": "No reliable end boundary."}))
    result = CliRunner().invoke(session, ["logs", "worker"])
    assert result.exit_code == 0
    assert "No reliable end boundary." in result.output
