"""Remote-safe dashboard links used by Discord delivery."""

from __future__ import annotations

import subprocess

from src.digest import build_digest
from src.digest.facts import DigestInputs, DigestWindow, WorkFact
from src.escalations import (
    EscalationFacts,
    MentionPolicy,
    render_resolution,
    render_resolved_root,
    render_root,
    render_thread_opener,
)
from src.remote_links import resolve_remote_link_base


def runner_for(outputs: dict[tuple[str, ...], tuple[int, str]]):
    def run(args, **_kwargs):
        code, stdout = outputs.get(tuple(args[1:]), (1, ""))
        return subprocess.CompletedProcess(args, code, stdout=stdout, stderr="")

    return run


def test_loopback_dashboard_url_uses_the_detected_tailscale_ipv4_and_keeps_url_parts():
    resolved = resolve_remote_link_base(
        "https://localhost:8443/dashboard?view=tasks#active",
        runner=runner_for({("ip", "-4"): (0, "100.99.1.2\n")}),
    )

    assert resolved.url == "https://100.99.1.2:8443/dashboard?view=tasks#active"
    assert not resolved.unavailable_notice


def test_loopback_ipv6_is_bracketed_and_tailscale_hostname_is_a_safe_fallback():
    ipv6 = resolve_remote_link_base(
        "http://[::1]:8081",
        runner=runner_for({("ip", "-6"): (0, "fd7a:115c:a1e0::55\n")}),
    )
    hostname = resolve_remote_link_base(
        "http://127.0.0.1:8081",
        runner=runner_for(
            {("status", "--json"): (0, '{"Self": {"DNSName": "queue.tailnet.ts.net."}}')}
        ),
    )

    assert ipv6.url == "http://[fd7a:115c:a1e0::55]:8081"
    assert hostname.url == "http://queue.tailnet.ts.net:8081"


def test_missing_or_malformed_tailscale_output_never_returns_a_misleading_loopback_url():
    missing = resolve_remote_link_base("http://localhost:8081", runner=runner_for({}))
    malformed = resolve_remote_link_base(
        "http://localhost:8081",
        runner=runner_for(
            {
                ("ip", "-4"): (0, "192.168.1.10\nnot-an-ip\n"),
                ("status", "--json"): (0, "not json"),
            }
        ),
    )

    for resolved in (missing, malformed):
        assert not resolved.url
        assert "Remote dashboard link unavailable" in resolved.unavailable_notice
    assert "localhost" not in resolved.unavailable_notice


def test_wildcard_bind_address_is_rewritten_instead_of_being_shared_as_a_remote_url():
    resolved = resolve_remote_link_base(
        "http://0.0.0.0:8081", runner=runner_for({("ip", "-4"): (0, "100.99.1.2")})
    )

    assert resolved.url == "http://100.99.1.2:8081"


def test_explicit_public_url_is_retained_without_tailscale_discovery_and_credentials_are_refused():
    untouched = resolve_remote_link_base(
        "https://queue.example.test/dashboard", runner=runner_for({})
    )
    credentials = resolve_remote_link_base(
        "https://user:secret@queue.example.test/dashboard", runner=runner_for({})
    )

    assert untouched.url == "https://queue.example.test/dashboard"
    assert not untouched.unavailable_notice
    assert not credentials.url
    assert "credentials" in credentials.unavailable_notice
    assert "secret" not in credentials.unavailable_notice


def test_all_discord_dashboard_summary_paths_render_the_remote_url_or_clear_limitation():
    facts = EscalationFacts(
        id="esc-1",
        project_id="project",
        state="needs_human",
        revision=0,
        severity="high",
        summary="blocked",
        investigation="checked the daemon",
        decision_requested="choose",
    )
    remote_base = "http://100.99.1.2:8081"
    expected = "http://100.99.1.2:8081/settings/messaging#escalation-reply-esc-1"
    rendered = (
        render_root(
            facts,
            mentions=MentionPolicy(),
            base_url=remote_base,
            dedup_key="root",
        ),
        render_resolved_root(facts, base_url=remote_base, dedup_key="resolved"),
        render_thread_opener(facts, base_url=remote_base, dedup_key="thread"),
        render_resolution(facts, base_url=remote_base, dedup_key="resolution"),
    )
    digest = build_digest(
        DigestInputs(
            window=DigestWindow(0, 3600),
            facts=(
                WorkFact(
                    key="completion:1",
                    kind="completed",
                    category="work",
                    project_id="project",
                    task_id="task",
                    title="task",
                    at=1,
                ),
            ),
        ),
        dashboard_notice="Remote dashboard link unavailable (Tailscale has no usable identity; open it on the daemon host).",
    )

    assert all(expected in text and "localhost" not in text for text in rendered)
    assert "Remote dashboard link unavailable" in digest.text
    assert "localhost" not in digest.text
