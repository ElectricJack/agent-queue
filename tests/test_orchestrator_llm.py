from src.config import DatabaseConfig, AppConfig, DiscordConfig, LLMConfig
from src.llm import LLMCallSpec, LLMClient
from src.orchestrator import Orchestrator
from tests.db_fixtures import lease_dsn


def test_orchestrator_builds_llm_client(tmp_path):
    cfg = AppConfig(
        discord=DiscordConfig(bot_token="t", guild_id="1"),
        workspace_dir=str(tmp_path / "w"),
        database=DatabaseConfig(url=lease_dsn("t.db")),
        data_dir=str(tmp_path / "d"),
        llm=LLMConfig(provider="google", model="gemini-2.5-flash"),
    )
    orch = Orchestrator(cfg)
    assert isinstance(orch.llm, LLMClient)
    assert orch.llm.config.provider == "google"
    assert orch.llm.resolve(LLMCallSpec()).model == "gemini-2.5-flash"
