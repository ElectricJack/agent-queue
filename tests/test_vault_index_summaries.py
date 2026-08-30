from src.llm import LLMClient
from src.llm.fake import FakeProvider
from src.vault_index import VaultIndexGenerator


async def test_summary_uses_llm_complete(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "a.md").write_text("# A\nsome knowledge\n")
    fake = FakeProvider()
    fake.add_text('"Covers A."')
    gen = VaultIndexGenerator(str(tmp_path))
    text = await gen._generate_summary(
        str(tmp_path / "memory"), LLMClient.with_provider(fake)
    )
    assert text == "Covers A."
    assert "concise technical writer" in fake.calls[0].system


async def test_summary_failure_returns_empty(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "a.md").write_text("x")
    gen = VaultIndexGenerator(str(tmp_path))
    assert (
        await gen._generate_summary(
            str(tmp_path / "memory"), LLMClient.with_provider(FakeProvider())
        )
        == ""
    )
