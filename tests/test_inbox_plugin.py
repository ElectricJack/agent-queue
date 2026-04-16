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
        hdr = (
            "mx.google.com; "
            "dkim=pass header.d=example.com; "
            "spf=fail; "
            "dmarc=fail"
        )
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
        hdr = (
            "mx.google.com; "
            "dkim=pass header.i=@example-co-uk.202501.gappssmtp.com; "
            "spf=pass"
        )
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
                    "payload": {"headers": [
                        {"name": "From", "value": "Jack <jack@example.com>"},
                        {"name": "Subject", "value": "hi"},
                        {"name": "Authentication-Results",
                         "value": "mx.google.com; dkim=pass header.d=example.com; spf=pass; dmarc=pass"},
                    ]},
                },
                "msg-stranger": {
                    "threadId": "thr-2",
                    "snippet": "stranger says hi",
                    "internalDate": "1700000001000",
                    "payload": {"headers": [
                        {"name": "From", "value": "stranger@random.org"},
                        {"name": "Subject", "value": "hello"},
                        {"name": "Authentication-Results",
                         "value": "mx.google.com; dkim=pass header.d=random.org; spf=pass; dmarc=pass"},
                    ]},
                },
                "msg-spoofed": {
                    "threadId": "thr-3",
                    "snippet": "pretending to be jack",
                    "internalDate": "1700000002000",
                    "payload": {"headers": [
                        {"name": "From", "value": "jack@example.com"},
                        {"name": "Subject", "value": "urgent"},
                        {"name": "Authentication-Results",
                         "value": "mx.google.com; dkim=pass header.d=attacker.com; spf=fail"},
                    ]},
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

        def get_message_metadata(self, msg_id):
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

    # Stranger: authenticated but not in allowlist.
    t, p = events_by_msg["msg-stranger"]
    assert t == EVENT_UNKNOWN
    assert "sender_not_in_allowlist" in p["classification_reasons"]

    # Spoofed: DKIM domain doesn't match From: domain, SPF failed.
    t, p = events_by_msg["msg-spoofed"]
    assert t == EVENT_UNKNOWN
    assert "spf_failed" in p["classification_reasons"]
    assert "dkim_domain_mismatch" in p["classification_reasons"]
