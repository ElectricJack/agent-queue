"""Tests for the aq-inbox plugin — auth parsing, allowlist classification, hot-reload."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.plugins.internal.inbox.allowlist import Allowlist
from src.plugins.internal.inbox.auth import (
    extract_from_address,
    parse_authentication_results,
)


# ---------------------------------------------------------------------------
# Authentication-Results parser
# ---------------------------------------------------------------------------


class TestAuthResultsParser:
    def test_gmail_all_pass(self):
        hdr = (
            "mx.google.com; "
            "dkim=pass header.i=@example.com header.s=selector; "
            "spf=pass (google.com: domain of sender@example.com ...) smtp.mailfrom=sender@example.com; "
            "dmarc=pass (p=REJECT sp=REJECT) header.from=example.com"
        )
        r = parse_authentication_results(hdr)
        assert r.spf_pass is True
        assert r.dkim_pass is True
        assert r.dmarc_pass is True
        assert r.dkim_signing_domain == "@example.com"

    def test_all_fail_by_default(self):
        r = parse_authentication_results("")
        assert r.spf_pass is False
        assert r.dkim_pass is False
        assert r.dmarc_pass is False
        assert r.dkim_signing_domain == ""

    def test_spf_fail_dkim_pass(self):
        hdr = "mx.google.com; dkim=pass header.d=example.com; spf=fail; dmarc=fail"
        r = parse_authentication_results(hdr)
        assert r.spf_pass is False
        assert r.dkim_pass is True
        assert r.dmarc_pass is False

    def test_dkim_domain_match_exact(self):
        hdr = "mx.google.com; dkim=pass header.i=@example.com; spf=pass; dmarc=pass"
        r = parse_authentication_results(hdr)
        assert r.dkim_matches_from_domain("User <user@example.com>") is True

    def test_dkim_domain_mismatch(self):
        hdr = "mx.google.com; dkim=pass header.i=@attacker.com; spf=pass; dmarc=pass"
        r = parse_authentication_results(hdr)
        assert r.dkim_matches_from_domain("user@example.com") is False

    def test_dkim_subdomain_relaxed_alignment(self):
        hdr = "mx.google.com; dkim=pass header.d=mail.example.com; spf=pass; dmarc=pass"
        r = parse_authentication_results(hdr)
        # From is example.com, DKIM signed by mail.example.com — should align.
        assert r.dkim_matches_from_domain("user@example.com") is True

    def test_empty_dkim_domain_never_matches(self):
        r = parse_authentication_results("spf=pass")
        assert r.dkim_matches_from_domain("user@example.com") is False

    def test_google_workspace_delegated_dkim_aligns(self):
        # Real-world example: Google Workspace customer without custom DKIM.
        # Gmail signs outbound as <from-domain-dashes>.<selector>.gappssmtp.com.
        hdr = (
            "mx.google.com; "
            "dkim=pass header.i=@mossandspade-com.20251104.gappssmtp.com header.s=20251104; "
            "spf=pass; arc=pass"
        )
        r = parse_authentication_results(hdr)
        assert r.dkim_matches_from_domain("Jessica <jessica@mossandspade.com>") is True

    def test_google_workspace_prefix_mismatch_does_not_align(self):
        # Attacker signs as a gappssmtp delegate for a DIFFERENT domain and
        # tries to impersonate mossandspade.com — must fail.
        hdr = (
            "mx.google.com; "
            "dkim=pass header.i=@attacker-com.20251104.gappssmtp.com header.s=20251104; "
            "spf=pass"
        )
        r = parse_authentication_results(hdr)
        assert r.dkim_matches_from_domain("jessica@mossandspade.com") is False

    def test_google_workspace_multi_label_domain(self):
        # example.co.uk -> example-co-uk.<sel>.gappssmtp.com
        hdr = "mx.google.com; dkim=pass header.i=@example-co-uk.202501.gappssmtp.com; spf=pass"
        r = parse_authentication_results(hdr)
        assert r.dkim_matches_from_domain("bob@example.co.uk") is True


class TestExtractFromAddress:
    def test_display_name_form(self):
        assert extract_from_address("Jack Kern <jack@example.com>") == "jack@example.com"

    def test_bare_address(self):
        assert extract_from_address("jack@example.com") == "jack@example.com"

    def test_lowercases(self):
        assert extract_from_address("Jack <JACK@Example.COM>") == "jack@example.com"

    def test_empty(self):
        assert extract_from_address("") == ""


# ---------------------------------------------------------------------------
# Allowlist classifier + hot-reload
# ---------------------------------------------------------------------------


def _write_allowlist(path: Path, *, senders=(), **settings) -> None:
    import yaml

    data = {
        "allowlist": [{"email": e} for e in senders],
        "settings": {
            "require_spf": settings.get("require_spf", True),
            "require_dkim": settings.get("require_dkim", True),
            "require_dkim_domain_match": settings.get("require_dkim_domain_match", True),
            "subject_prefix": settings.get("subject_prefix", ""),
            "poll_interval_seconds": settings.get("poll_interval_seconds", 30),
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data))


class TestAllowlistClassifier:
    def test_ensure_exists_creates_template(self, tmp_path):
        f = tmp_path / "inbox" / "allowlist.yaml"
        al = Allowlist(f)
        created = al.ensure_exists("proj-a")
        assert created is True
        assert f.exists()
        assert "proj-a" in f.read_text()
        # Calling again is a no-op.
        assert al.ensure_exists("proj-a") is False

    def test_allowlisted_when_everything_passes(self, tmp_path):
        f = tmp_path / "allowlist.yaml"
        _write_allowlist(f, senders=["jack@example.com"])
        al = Allowlist(f)
        d = al.classify(
            from_addr="jack@example.com",
            subject="hello",
            spf_pass=True,
            dkim_pass=True,
            dkim_domain_matches=True,
        )
        assert d.allowlisted is True
        assert d.reasons == ()
        assert d.matched_entry is not None
        assert d.matched_entry.email == "jack@example.com"

    def test_not_in_allowlist_is_unknown(self, tmp_path):
        f = tmp_path / "allowlist.yaml"
        _write_allowlist(f, senders=["jack@example.com"])
        al = Allowlist(f)
        d = al.classify(
            from_addr="stranger@example.com",
            subject="hi",
            spf_pass=True,
            dkim_pass=True,
            dkim_domain_matches=True,
        )
        assert d.allowlisted is False
        assert "sender_not_in_allowlist" in d.reasons

    def test_allowlisted_but_spf_fail_is_unknown(self, tmp_path):
        f = tmp_path / "allowlist.yaml"
        _write_allowlist(f, senders=["jack@example.com"])
        al = Allowlist(f)
        d = al.classify(
            from_addr="jack@example.com",
            subject="hi",
            spf_pass=False,
            dkim_pass=True,
            dkim_domain_matches=True,
        )
        assert d.allowlisted is False
        assert "spf_failed" in d.reasons

    def test_allowlisted_but_dkim_fail_is_unknown(self, tmp_path):
        f = tmp_path / "allowlist.yaml"
        _write_allowlist(f, senders=["jack@example.com"])
        al = Allowlist(f)
        d = al.classify(
            from_addr="jack@example.com",
            subject="hi",
            spf_pass=True,
            dkim_pass=False,
            dkim_domain_matches=True,
        )
        assert d.allowlisted is False
        assert "dkim_failed" in d.reasons

    def test_dkim_domain_mismatch_is_unknown(self, tmp_path):
        f = tmp_path / "allowlist.yaml"
        _write_allowlist(f, senders=["jack@example.com"])
        al = Allowlist(f)
        d = al.classify(
            from_addr="jack@example.com",
            subject="hi",
            spf_pass=True,
            dkim_pass=True,
            dkim_domain_matches=False,
        )
        assert d.allowlisted is False
        assert "dkim_domain_mismatch" in d.reasons

    def test_missing_subject_prefix_when_required(self, tmp_path):
        f = tmp_path / "allowlist.yaml"
        _write_allowlist(f, senders=["jack@example.com"], subject_prefix="[AQ]")
        al = Allowlist(f)
        d = al.classify(
            from_addr="jack@example.com",
            subject="hi",
            spf_pass=True,
            dkim_pass=True,
            dkim_domain_matches=True,
        )
        assert d.allowlisted is False
        assert "subject_prefix_missing" in d.reasons

    def test_subject_prefix_present_passes(self, tmp_path):
        f = tmp_path / "allowlist.yaml"
        _write_allowlist(f, senders=["jack@example.com"], subject_prefix="[AQ]")
        al = Allowlist(f)
        d = al.classify(
            from_addr="jack@example.com",
            subject="[AQ] please help",
            spf_pass=True,
            dkim_pass=True,
            dkim_domain_matches=True,
        )
        assert d.allowlisted is True

    def test_require_spf_disabled_bypasses_spf_check(self, tmp_path):
        f = tmp_path / "allowlist.yaml"
        _write_allowlist(f, senders=["jack@example.com"], require_spf=False)
        al = Allowlist(f)
        d = al.classify(
            from_addr="jack@example.com",
            subject="hi",
            spf_pass=False,
            dkim_pass=True,
            dkim_domain_matches=True,
        )
        assert d.allowlisted is True

    def test_hot_reload_on_mtime_change(self, tmp_path):
        f = tmp_path / "allowlist.yaml"
        _write_allowlist(f, senders=["jack@example.com"])
        al = Allowlist(f)
        assert "jack@example.com" in al.entries

        # Wait briefly so mtime can change (some filesystems have 1s resolution).
        time.sleep(1.1)
        _write_allowlist(f, senders=["jack@example.com", "jessica@example.com"])
        # Reloads lazily on next access.
        assert "jessica@example.com" in al.entries
        assert len(al.entries) == 2

    def test_missing_file_gives_empty_allowlist(self, tmp_path):
        f = tmp_path / "missing.yaml"
        al = Allowlist(f)
        assert al.entries == {}
        d = al.classify(
            from_addr="x@y.com",
            subject="",
            spf_pass=True,
            dkim_pass=True,
            dkim_domain_matches=True,
        )
        assert d.allowlisted is False
        assert "sender_not_in_allowlist" in d.reasons

    def test_case_insensitive_sender_match(self, tmp_path):
        f = tmp_path / "allowlist.yaml"
        _write_allowlist(f, senders=["jack@example.com"])
        al = Allowlist(f)
        d = al.classify(
            from_addr="JACK@EXAMPLE.COM",
            subject="hi",
            spf_pass=True,
            dkim_pass=True,
            dkim_domain_matches=True,
        )
        # classify() lowercases internally
        assert d.allowlisted is True


# ---------------------------------------------------------------------------
# Poller end-to-end with injected Gmail client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poller_emits_correct_event_types(tmp_path):
    """Simulate a poll cycle with a stub Gmail client and verify event routing.

    Verifies the plugin's core contract: ``email.received.allowlisted``
    vs ``email.received.unknown`` is chosen deterministically by the
    allowlist + auth headers, independent of any LLM.
    """
    from src.plugins.internal.inbox.poller import (
        EVENT_ALLOWLISTED,
        EVENT_UNKNOWN,
        InboxPoller,
    )

    f = tmp_path / "allowlist.yaml"
    _write_allowlist(f, senders=["jack@example.com"])
    al = Allowlist(f)

    class StubGmail:
        _config = type("c", (), {"user_id": "me"})()

        def __init__(self):
            self._messages = {
                "msg-good": {
                    "threadId": "thr-1",
                    "snippet": "hello",
                    "internalDate": "1700000000000",
                    # base64url("hello world\nfull body here") = "aGVsbG8gd29ybGQKZnVsbCBib2R5IGhlcmU="
                    "payload": {
                        "mimeType": "text/plain",
                        "headers": [
                            {"name": "From", "value": "Jack <jack@example.com>"},
                            {"name": "Subject", "value": "hi"},
                            {
                                "name": "Authentication-Results",
                                "value": "mx.google.com; dkim=pass header.d=example.com; spf=pass; dmarc=pass",
                            },
                        ],
                        "body": {"data": "aGVsbG8gd29ybGQKZnVsbCBib2R5IGhlcmU="},
                    },
                },
                "msg-stranger": {
                    "threadId": "thr-2",
                    "snippet": "stranger says hi",
                    "internalDate": "1700000001000",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "stranger@random.org"},
                            {"name": "Subject", "value": "hello"},
                            {
                                "name": "Authentication-Results",
                                "value": "mx.google.com; dkim=pass header.d=random.org; spf=pass; dmarc=pass",
                            },
                        ]
                    },
                },
                "msg-spoofed": {
                    "threadId": "thr-3",
                    "snippet": "pretending to be jack",
                    "internalDate": "1700000002000",
                    "payload": {
                        "headers": [
                            {"name": "From", "value": "jack@example.com"},
                            {"name": "Subject", "value": "urgent"},
                            {
                                "name": "Authentication-Results",
                                "value": "mx.google.com; dkim=pass header.d=attacker.com; spf=fail",
                            },
                        ]
                    },
                },
            }

        def _ensure_service(self):
            class _S:
                def users(self_inner):
                    class _U:
                        def getProfile(self_u, userId):
                            class _G:
                                def execute(self_g):
                                    return {"historyId": "0"}

                            return _G()

                    return _U()

            return _S()

        def list_new_message_ids(self, *, history_id, query):
            return list(self._messages.keys()), "999"

        def get_message(self, msg_id):
            return self._messages[msg_id]

        def mark_read(self, msg_id):
            pass

    emitted: list[tuple[str, dict]] = []

    async def emit(event_type, payload):
        emitted.append((event_type, payload))

    poller = InboxPoller(
        project_id="test-proj",
        gmail=StubGmail(),
        allowlist=al,
        emit=emit,
        mark_read_on_emit=False,
    )
    # Skip the real bootstrap (which needs a live Gmail profile call).
    poller._history_id = "0"
    await poller._poll_once()

    events_by_msg = {p["message_id"]: (t, p) for t, p in emitted}

    # Allowlisted: in list, SPF+DKIM pass, DKIM domain matches From.
    t, p = events_by_msg["msg-good"]
    assert t == EVENT_ALLOWLISTED
    assert p["classification_reasons"] == []
    assert p["from"] == "jack@example.com"
    assert p["full_body"] == "hello world\nfull body here"

    # Stranger: authenticated but not in allowlist.
    t, p = events_by_msg["msg-stranger"]
    assert t == EVENT_UNKNOWN
    assert "sender_not_in_allowlist" in p["classification_reasons"]

    # Spoofed: DKIM domain doesn't match From: domain, SPF failed.
    t, p = events_by_msg["msg-spoofed"]
    assert t == EVENT_UNKNOWN
    assert "spf_failed" in p["classification_reasons"]
    assert "dkim_domain_mismatch" in p["classification_reasons"]


@pytest.mark.asyncio
async def test_transient_get_message_failure_does_not_advance_history_or_seen(tmp_path):
    """A fetch failure must remain retryable on the next polling cycle."""
    from src.plugins.internal.inbox.poller import InboxPoller

    allowlist_path = tmp_path / "allowlist.yaml"
    _write_allowlist(allowlist_path, senders=["jack@example.com"])

    class StubGmail:
        def __init__(self):
            self.calls = 0

        def list_new_message_ids(self, *, history_id, query):
            return ["X"], "next-history"

        def get_message(self, message_id):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("temporary Gmail failure")
            return {
                "threadId": "thread",
                "internalDate": "0",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "jack@example.com"},
                        {"name": "Subject", "value": "hello"},
                        {
                            "name": "Authentication-Results",
                            "value": "spf=pass; dkim=pass header.d=example.com",
                        },
                    ]
                },
            }

    emitted = []

    async def emit(event_type, payload):
        emitted.append((event_type, payload))

    poller = InboxPoller(
        project_id="project",
        gmail=StubGmail(),
        allowlist=Allowlist(allowlist_path),
        emit=emit,
        mark_read_on_emit=False,
    )
    poller._history_id = "initial"
    await poller._poll_once()
    assert poller._history_id == "initial"
    assert "X" not in poller._seen_ids

    await poller._poll_once()
    assert poller._history_id == "next-history"
    assert "X" in poller._seen_ids
    assert emitted[0][1]["message_id"] == "X"


# ---------------------------------------------------------------------------
# Allowlist parse-failure paths (coverage plan §plugins items 25-26)
# ---------------------------------------------------------------------------


class TestAllowlistParseFailures:
    def test_allowlist_survives_corrupt_yaml_without_widening_access(self, tmp_path, caplog):
        """A corrupted allowlist file keeps the previous in-memory list
        live (no senders gained, no senders granted) and logs an error."""
        import logging
        import os

        f = tmp_path / "allowlist.yaml"
        _write_allowlist(f, senders=["jack@example.com"])
        al = Allowlist(f)
        assert set(al.entries) == {"jack@example.com"}

        f.write_text("allowlist: [unclosed\n", encoding="utf-8")
        os.utime(f, (time.time() + 10, time.time() + 10))

        with caplog.at_level(logging.ERROR, logger="src.plugins.internal.inbox.allowlist"):
            entries = al.entries

        assert "parse failed" in caplog.text
        # No sender gained; the previous list is still what's live.
        assert set(entries) == {"jack@example.com"}
        decision = al.classify(
            from_addr="stranger@evil.org",
            subject="hi",
            spf_pass=True,
            dkim_pass=True,
            dkim_domain_matches=True,
        )
        assert decision.allowlisted is False
        assert "sender_not_in_allowlist" in decision.reasons

    def test_allowlist_skips_malformed_entries(self, tmp_path, caplog):
        """Malformed allowlist entries are skipped with warnings and are
        never treated as allowlisted."""
        import logging

        f = tmp_path / "allowlist.yaml"
        f.write_text(
            "allowlist:\n"
            "  - just-a-string\n"
            "  - note: no email key\n"
            "  - email: not-an-address\n"
            "  - email: valid@example.com\n",
            encoding="utf-8",
        )
        al = Allowlist(f)

        with caplog.at_level(logging.WARNING, logger="src.plugins.internal.inbox.allowlist"):
            entries = al.entries

        assert set(entries) == {"valid@example.com"}
        skip_warnings = [r for r in caplog.records if "skipping" in r.getMessage()]
        assert len(skip_warnings) == 3

        for sender in ("just-a-string", "not-an-address"):
            decision = al.classify(
                from_addr=sender,
                subject="s",
                spf_pass=True,
                dkim_pass=True,
                dkim_domain_matches=True,
            )
            assert decision.allowlisted is False


# ---------------------------------------------------------------------------
# Poller lifecycle (coverage plan §plugins items 27-29 + PLG-3)
# ---------------------------------------------------------------------------


class _ProfileStubGmail:
    """Minimal Gmail stub whose bootstrap profile call works."""

    _config = type("c", (), {"user_id": "me"})()

    def _ensure_service(self):
        class _Service:
            def users(self_s):
                class _Users:
                    def getProfile(self_u, userId):
                        class _Get:
                            def execute(self_g):
                                return {"historyId": "0"}

                        return _Get()

                return _Users()

        return _Service()

    def list_new_message_ids(self, *, history_id, query):
        return [], history_id or "0"

    def get_message(self, msg_id):
        raise AssertionError("not expected in this test")

    def mark_read(self, msg_id):
        pass


@pytest.mark.asyncio
async def test_poller_start_stop_is_idempotent_and_cancels_task(tmp_path):
    """start() twice creates one task; stop() cancels it and a second
    stop() is a harmless no-op."""
    from src.plugins.internal.inbox.poller import InboxPoller

    allowlist_path = tmp_path / "allowlist.yaml"
    _write_allowlist(allowlist_path, senders=[])

    emitted = []

    async def emit(event_type, payload):
        emitted.append((event_type, payload))

    poller = InboxPoller(
        project_id="proj",
        gmail=_ProfileStubGmail(),
        allowlist=Allowlist(allowlist_path),
        emit=emit,
        mark_read_on_emit=False,
    )

    await poller.start()
    first_task = poller._task
    assert first_task is not None

    await poller.start()
    assert poller._task is first_task, "second start() must not spawn a new task"

    await poller.stop()
    assert poller._task is None
    assert poller._running is False
    assert first_task.done()

    # Second stop() is a no-op and must not raise.
    await poller.stop()
    assert poller._task is None


@pytest.mark.asyncio
async def test_poll_failure_is_counted_and_loop_continues(tmp_path, monkeypatch):
    """A transient Gmail error is counted into stats["errors"] and the
    loop keeps polling — the next cycle delivers its event."""
    import asyncio as _asyncio

    from src.plugins.internal.inbox.poller import EVENT_ALLOWLISTED, InboxPoller

    allowlist_path = tmp_path / "allowlist.yaml"
    _write_allowlist(allowlist_path, senders=["jack@example.com"])

    class FlakyGmail(_ProfileStubGmail):
        def __init__(self):
            self.list_calls = 0

        def list_new_message_ids(self, *, history_id, query):
            self.list_calls += 1
            if self.list_calls == 1:
                raise RuntimeError("gmail 500")
            return ["m1"], "h2"

        def get_message(self, msg_id):
            return {
                "threadId": "thr",
                "internalDate": "0",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "jack@example.com"},
                        {"name": "Subject", "value": "hello"},
                        {
                            "name": "Authentication-Results",
                            "value": "spf=pass; dkim=pass header.d=example.com",
                        },
                    ]
                },
            }

    emitted = []

    async def emit(event_type, payload):
        emitted.append((event_type, payload))

    poller = InboxPoller(
        project_id="proj",
        gmail=FlakyGmail(),
        allowlist=Allowlist(allowlist_path),
        emit=emit,
        mark_read_on_emit=False,
    )

    sleeps = {"count": 0}
    real_sleep = _asyncio.sleep

    async def fake_sleep(seconds):
        sleeps["count"] += 1
        if sleeps["count"] >= 2:
            poller._running = False
        await real_sleep(0)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    poller._running = True
    await poller._loop()

    assert poller.stats["errors"] == 1
    assert poller.stats["polls"] == 2
    assert len(emitted) == 1
    assert emitted[0][0] == EVENT_ALLOWLISTED
    assert emitted[0][1]["message_id"] == "m1"


@pytest.mark.asyncio
async def test_plugin_does_not_start_pollers_when_disabled_or_projectless(monkeypatch):
    """InboxPlugin.initialize starts nothing (and never touches Gmail
    credentials) when disabled or when no projects are configured."""
    from src.plugins.base import PluginContext, TrustLevel
    from src.plugins.internal.inbox import plugin as inbox_plugin_mod
    from unittest.mock import AsyncMock, MagicMock

    class ExplodingGmailClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("GmailClient must not be constructed")

    monkeypatch.setattr(inbox_plugin_mod, "GmailClient", ExplodingGmailClient)

    def make_ctx(tmp_base, inbox_cfg):
        config_svc = MagicMock()
        config_svc.inbox = inbox_cfg
        config_svc.data_dir = str(tmp_base)
        db = AsyncMock()
        bus = MagicMock()
        bus.emit = AsyncMock()
        return PluginContext(
            plugin_name="aq-inbox",
            install_path=str(tmp_base / "install"),
            data_path=str(tmp_base / "data"),
            db=db,
            bus=bus,
            command_registry={},
            tool_registry={},
            event_type_registry=set(),
            trust_level=TrustLevel.INTERNAL,
            services={"config": config_svc},
        )

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        plugin = inbox_plugin_mod.InboxPlugin()
        await plugin.initialize(make_ctx(base / "a", {"enabled": False}))
        assert plugin._pollers == []

        plugin = inbox_plugin_mod.InboxPlugin()
        await plugin.initialize(make_ctx(base / "b", {"enabled": True, "projects": []}))
        assert plugin._pollers == []


@pytest.mark.asyncio
async def test_seen_ids_cap_keeps_most_recent_ids(tmp_path):
    """PLG-3: the bounded dedup structure evicts the *oldest* ids on
    trim — a just-processed id must survive, so it can never re-emit."""
    from src.plugins.internal.inbox.poller import InboxPoller

    allowlist_path = tmp_path / "allowlist.yaml"
    _write_allowlist(allowlist_path, senders=[])

    class OneMessageGmail(_ProfileStubGmail):
        def list_new_message_ids(self, *, history_id, query):
            return ["fresh"], "h2"

        def get_message(self, msg_id):
            return {
                "threadId": "thr",
                "internalDate": "0",
                "payload": {"headers": [{"name": "From", "value": "x@y.z"}]},
            }

    emitted = []

    async def emit(event_type, payload):
        emitted.append((event_type, payload))

    poller = InboxPoller(
        project_id="proj",
        gmail=OneMessageGmail(),
        allowlist=Allowlist(allowlist_path),
        emit=emit,
        mark_read_on_emit=False,
    )
    poller._history_id = "h1"
    # Simulate a long-running poller: 10,001 previously-seen ids in order.
    poller._seen_ids = dict.fromkeys(f"m{i}" for i in range(10001))

    await poller._poll_once()

    assert len(poller._seen_ids) == 5000
    assert "fresh" in poller._seen_ids, "just-processed id was evicted by the cap"
    # The survivors are exactly the most recently inserted ids.
    assert "m10000" in poller._seen_ids
    assert "m5002" in poller._seen_ids
    assert "m5001" not in poller._seen_ids
    assert "m0" not in poller._seen_ids

    # And the id keeps deduplicating: a second poll of the same message
    # emits nothing new.
    await poller._poll_once()
    assert len(emitted) == 1
