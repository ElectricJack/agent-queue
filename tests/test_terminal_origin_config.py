import pytest
import yaml

from src.config import ApiAuthConfig, load_config
from src.config_editor import build_config_schema


def test_terminal_origins_load_and_are_editable(tmp_path):
    origins = ["http://localhost:5173", "https://dashboard.example", "http://[::1]:5173"]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "discord": {"bot_token": "test-token", "guild_id": "1"},
        "database_path": str(tmp_path / "state.db"),
        "api_auth": {"trusted_dashboard_origins": origins},
    }))
    config = load_config(str(path))
    assert config.api_auth.trusted_dashboard_origins == origins
    assert config.api_auth.validate() == []
    schema = build_config_schema()["properties"]["api_auth"]["properties"]
    assert schema["trusted_dashboard_origins"]["type"] == "array"
    assert schema["trusted_dashboard_origins"]["items"] == {"type": "string"}


@pytest.mark.parametrize("origins", [
    "*", None, ["*"], ["null"], ["file:///tmp/dashboard"],
    ["https://dashboard.example/path"], ["https://user:private@dashboard.example"],
    ["https://dashboard.example:bad"], ["https://dashboard.example?"],
    ["https://dashboard.example#"], ["https://dashboard.example\n"], [42],
])
def test_terminal_origins_reject_unsafe_or_ambiguous_values(origins):
    config = ApiAuthConfig(trusted_dashboard_origins=origins)
    errors = config.validate()
    assert any(error.field == "trusted_dashboard_origins" for error in errors)


def test_terminal_origin_defaults_do_not_allow_cross_origin_access():
    assert ApiAuthConfig().trusted_dashboard_origins == []
    first = ApiAuthConfig()
    first.trusted_dashboard_origins.append("https://dashboard.example")
    assert ApiAuthConfig().trusted_dashboard_origins == []
