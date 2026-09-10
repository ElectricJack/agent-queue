"""Literal-secret redaction for the Config API and its write-path inverse.

Every fixture here is a fake: the "credentials" are obvious sentinels, no test
reads or writes the operator's ``~/.agent-queue/config.yaml``, and nothing
prints a live value.
"""

from __future__ import annotations

import pytest
import yaml

from src.config_secrets import (
    SECRET_PLACEHOLDER,
    is_secret_key,
    redact_config,
    restore_section_secrets,
)

# Obvious fakes, shaped like the real thing so the value heuristics fire.
FAKE_DSN = "postgresql+asyncpg://aq_user:fake-db-password@db.internal:5432/agent_queue"
FAKE_BOT_TOKEN = "fake-literal-discord-token"
FAKE_OPENAI_KEY = "sk-fakefakefakefakefakefake0123456789"
FAKE_GH_TOKEN = "ghp_fakefakefakefakefakefake0123456789"


class TestIsSecretKey:
    @pytest.mark.parametrize(
        "key",
        [
            "bot_token",
            "api_key",
            "embedding_api_key",
            "milvus_token",
            "password",
            "client_secret",
            "private_key",
            "auth_token",
            "dsn",
        ],
    )
    def test_credential_keys(self, key):
        assert is_secret_key(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            # Knobs *about* tokens — numeric in practice, but a quoted number
            # in YAML must not be mistaken for a credential either.
            "max_tokens",
            "context_max_tokens",
            "token_ttl_hours",
            "token_window_seconds",
            "token_exhaustion_retry_seconds",
            "global_token_budget_daily",
            "max_daily_playbook_tokens",
            # A path to a key file is not the key.
            "private_key_path",
            "negative_private_key_path",
            # Identity, not credential.
            "authorized_users",
            "guild_id",
            "require_session_token_enabled",
        ],
    )
    def test_non_credential_keys(self, key):
        assert is_secret_key(key) is False


class TestRedactConfig:
    def test_literal_secret_key_is_replaced_and_reported(self):
        raw = {"discord": {"bot_token": FAKE_BOT_TOKEN, "guild_id": "123"}}
        redacted, paths = redact_config(raw)
        assert redacted["discord"]["bot_token"] == SECRET_PLACEHOLDER
        assert redacted["discord"]["guild_id"] == "123"
        assert paths == ["discord.bot_token"]
        assert FAKE_BOT_TOKEN not in yaml.safe_dump(redacted)

    def test_input_document_is_not_mutated(self):
        raw = {"discord": {"bot_token": FAKE_BOT_TOKEN}}
        redact_config(raw)
        assert raw["discord"]["bot_token"] == FAKE_BOT_TOKEN

    def test_dsn_password_only_is_replaced(self):
        redacted, paths = redact_config({"database": {"url": FAKE_DSN}})
        url = redacted["database"]["url"]
        assert url == (
            f"postgresql+asyncpg://aq_user:{SECRET_PLACEHOLDER}@db.internal:5432/agent_queue"
        )
        # The diagnosable parts survive; the password does not.
        assert "aq_user" in url and "db.internal:5432" in url and "agent_queue" in url
        assert "fake-db-password" not in url
        assert paths == ["database.url"]

    def test_dsn_without_credentials_is_untouched(self):
        plain = "postgresql+asyncpg://localhost:5432/agent_queue"
        redacted, paths = redact_config({"database": {"url": plain}})
        assert redacted["database"]["url"] == plain
        assert paths == []

    def test_env_var_reference_is_preserved(self):
        raw = {
            "discord": {"bot_token": "${DISCORD_BOT_TOKEN}"},
            "database": {"url": "postgresql://aq:${PGPASSWORD}@db/aq"},
        }
        redacted, paths = redact_config(raw)
        assert redacted["discord"]["bot_token"] == "${DISCORD_BOT_TOKEN}"
        assert redacted["database"]["url"] == "postgresql://aq:${PGPASSWORD}@db/aq"
        assert paths == []

    def test_empty_string_secret_is_left_alone(self):
        redacted, paths = redact_config({"llm": {"api_key": ""}})
        assert redacted["llm"]["api_key"] == ""
        assert paths == []

    def test_numeric_token_knobs_survive(self):
        raw = {
            "llm": {"max_tokens": 4096},
            "api_auth": {"token_ttl_hours": 72, "require_session_token": False},
            "metrics": {"token_window_seconds": 300.0},
        }
        redacted, paths = redact_config(raw)
        assert redacted == raw
        assert paths == []

    def test_provider_token_shape_is_caught_under_an_innocuous_key(self):
        raw = {"notes": {"scratch": FAKE_OPENAI_KEY, "other": FAKE_GH_TOKEN}}
        redacted, paths = redact_config(raw)
        assert redacted["notes"]["scratch"] == SECRET_PLACEHOLDER
        assert redacted["notes"]["other"] == SECRET_PLACEHOLDER
        assert sorted(paths) == ["notes.other", "notes.scratch"]

    def test_pem_block_is_caught(self):
        raw = {"integration": {"inline": "-----BEGIN RSA PRIVATE KEY-----\nfake"}}
        redacted, paths = redact_config(raw)
        assert redacted["integration"]["inline"] == SECRET_PLACEHOLDER
        assert paths == ["integration.inline"]

    def test_nested_and_list_paths_are_reported_with_indexes(self):
        raw = {"llm": {"providers": [{"api_key": FAKE_OPENAI_KEY}, {"api_key": "${OPENAI_KEY}"}]}}
        redacted, paths = redact_config(raw)
        assert redacted["llm"]["providers"][0]["api_key"] == SECRET_PLACEHOLDER
        assert redacted["llm"]["providers"][1]["api_key"] == "${OPENAI_KEY}"
        assert paths == ["llm.providers[0].api_key"]

    def test_list_items_inherit_the_parent_secret_key(self):
        raw = {"plugins": {"api_keys": ["fake-one", "fake-two"]}}
        redacted, paths = redact_config(raw)
        assert redacted["plugins"]["api_keys"] == [SECRET_PLACEHOLDER, SECRET_PLACEHOLDER]
        assert paths == ["plugins.api_keys[0]", "plugins.api_keys[1]"]


class TestRestoreSectionSecrets:
    def test_untouched_placeholder_restores_the_stored_literal(self):
        stored = {"bot_token": FAKE_BOT_TOKEN, "guild_id": "123"}
        incoming = {"bot_token": SECRET_PLACEHOLDER, "guild_id": "456"}
        restored, unresolved = restore_section_secrets(incoming, stored)
        assert restored == {"bot_token": FAKE_BOT_TOKEN, "guild_id": "456"}
        assert unresolved == []

    def test_deliberate_new_credential_is_written_through(self):
        stored = {"bot_token": FAKE_BOT_TOKEN}
        restored, unresolved = restore_section_secrets({"bot_token": "fake-rotated"}, stored)
        assert restored == {"bot_token": "fake-rotated"}
        assert unresolved == []

    def test_dsn_password_is_spliced_back_around_other_edits(self):
        stored = {"url": FAKE_DSN}
        incoming = {
            "url": f"postgresql+asyncpg://aq_user:{SECRET_PLACEHOLDER}@db.internal:5432/other_db"
        }
        restored, unresolved = restore_section_secrets(incoming, stored)
        assert restored["url"] == (
            "postgresql+asyncpg://aq_user:fake-db-password@db.internal:5432/other_db"
        )
        assert unresolved == []

    def test_placeholder_with_nothing_stored_is_unresolved(self):
        restored, unresolved = restore_section_secrets({"bot_token": SECRET_PLACEHOLDER}, None)
        assert unresolved == ["bot_token"]
        # The caller refuses the write; the sentinel is handed back untouched
        # rather than silently dropped.
        assert restored == {"bot_token": SECRET_PLACEHOLDER}

    def test_renamed_key_carrying_a_placeholder_is_unresolved(self):
        stored = {"bot_token": FAKE_BOT_TOKEN}
        _, unresolved = restore_section_secrets({"bot_token_new": SECRET_PLACEHOLDER}, stored)
        assert unresolved == ["bot_token_new"]

    def test_hand_pasted_placeholder_inside_a_plain_string_is_unresolved(self):
        stored = {"note": "hello"}
        _, unresolved = restore_section_secrets({"note": f"hello {SECRET_PLACEHOLDER}"}, stored)
        assert unresolved == ["note"]

    def test_dsn_placeholder_with_no_stored_password_is_unresolved(self):
        stored = {"url": "postgresql+asyncpg://localhost:5432/agent_queue"}
        incoming = {"url": f"postgresql+asyncpg://aq:{SECRET_PLACEHOLDER}@localhost:5432/aq"}
        _, unresolved = restore_section_secrets(incoming, stored)
        assert unresolved == ["url"]

    def test_nested_and_list_positions_restore_by_path(self):
        stored = {"providers": [{"api_key": FAKE_OPENAI_KEY}, {"api_key": "fake-second"}]}
        incoming = {
            "providers": [
                {"api_key": SECRET_PLACEHOLDER, "model": "claude-opus-5"},
                {"api_key": SECRET_PLACEHOLDER},
            ]
        }
        restored, unresolved = restore_section_secrets(incoming, stored)
        assert restored["providers"][0] == {"api_key": FAKE_OPENAI_KEY, "model": "claude-opus-5"}
        assert restored["providers"][1] == {"api_key": "fake-second"}
        assert unresolved == []

    def test_reordered_list_is_reported_rather_than_mismatched(self):
        stored = {"providers": [{"api_key": FAKE_OPENAI_KEY}]}
        incoming = {"providers": [{"name": "new"}, {"api_key": SECRET_PLACEHOLDER}]}
        _, unresolved = restore_section_secrets(incoming, stored)
        assert unresolved == ["providers[1].api_key"]

    def test_section_without_placeholders_passes_through_unchanged(self):
        incoming = {"rolling_window_hours": 48}
        restored, unresolved = restore_section_secrets(incoming, {"rolling_window_hours": 24})
        assert restored is incoming
        assert unresolved == []

    def test_redact_then_restore_is_the_identity_on_an_untouched_section(self):
        section = {
            "url": FAKE_DSN,
            "pool_min_size": 1,
            "note": "${SOME_VAR}",
        }
        redacted, _ = redact_config(section)
        restored, unresolved = restore_section_secrets(redacted, section)
        assert restored == section
        assert unresolved == []
