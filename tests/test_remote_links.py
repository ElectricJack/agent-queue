"""The one dashboard origin every link sent off this machine names.

tailscale-dashboard-link spec §4-§5: an explicit, validated
``dashboard.server.public_url`` (alias ``dashboard.public_url``); a direct
tailnet bind only when ``tailscale ip`` confirms it; otherwise an unavailable
notice with the configuration reason.  ``health_check.base_url`` is never a
dashboard link, a loopback bind is never rewritten to a Tailscale address,
and every sender renders the same origin.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from src.config import (
    ApiAuthConfig,
    DashboardServerConfig,
    HealthCheckConfig,
    dashboard_server_config_from_raw,
    load_config,
)
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
from src.remote_links import (
    ALIAS_KEY,
    CANONICAL_KEY,
    HOST_KEY,
    NEGATIVE_TTL_SECONDS,
    POSITIVE_TTL_SECONDS,
    REMOTE_REACHABILITY,
    DashboardLinkResolver,
    DashboardLinkSettings,
    StaticDashboardLink,
    TailscaleProbe,
    check_public_url,
    dashboard_link_base,
    diagnose_dashboard_link,
    normalise_origin,
    probe_tailscale,
)
from src.reviews.notifier import ReviewNotifier


@pytest.fixture(autouse=True)
def _pg_backend():
    """Pure resolution, config parsing and fake transports; no test database."""


class Probes:
    """A fake ``tailscale ip`` that records each call and answers in turn."""

    def __init__(self, *answers: TailscaleProbe) -> None:
        self.answers = list(answers) or [TailscaleProbe("missing", detail="not on PATH")]
        self.calls: list[str] = []

    async def __call__(self, tailscale_path: str) -> TailscaleProbe:
        self.calls.append(tailscale_path)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def tailnet(*addresses: str) -> TailscaleProbe:
    return TailscaleProbe("available", addresses=addresses)


def resolve(probe: Probes | None = None, **settings) -> object:
    return asyncio.run(
        dashboard_link_base(DashboardLinkSettings(**settings), probe=probe or Probes())
    )


def app_config(**server) -> SimpleNamespace:
    trusted = server.pop("trusted", [])
    return SimpleNamespace(
        dashboard_server=DashboardServerConfig(**server),
        api_auth=ApiAuthConfig(trusted_dashboard_origins=trusted),
        health_check=HealthCheckConfig(base_url="http://100.99.1.2:8081"),
    )


# ---------------------------------------------------------------------------
# 1. An explicit public URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "origin"),
    [
        ("https://aq.tailnet.ts.net", "https://aq.tailnet.ts.net"),
        ("https://AQ.Tailnet.ts.net/", "https://aq.tailnet.ts.net"),
        ("https://aq.tailnet.ts.net:443", "https://aq.tailnet.ts.net"),
        ("http://aq.tailnet.ts.net:80/", "http://aq.tailnet.ts.net"),
        ("https://aq.tailnet.ts.net:8443", "https://aq.tailnet.ts.net:8443"),
        ("http://100.99.1.2:8082", "http://100.99.1.2:8082"),
        ("http://[fd7a:115c:a1e0::55]:8082/", "http://[fd7a:115c:a1e0::55]:8082"),
    ],
)
def test_a_valid_public_url_is_used_exactly_as_its_normalised_origin(configured, origin):
    probe = Probes()

    link = resolve(probe, public_url=configured)

    assert link.url == origin
    assert link.reason == "public_url" and link.source == CANONICAL_KEY
    assert not link.notice
    # A configured URL needs no Tailscale CLI at all.
    assert probe.calls == []


@pytest.mark.parametrize(
    ("configured", "problem"),
    [
        ("https://user:hunter2@aq.example.test", "must not contain credentials"),
        ("https://token@aq.example.test", "must not contain credentials"),
        ("https://aq.example.test/?token=hunter2", "must not contain a query or fragment"),
        ("https://aq.example.test/#frag", "must not contain a query or fragment"),
        ("https://aq.example.test/dashboard", "must be an origin with no path"),
        ("ftp://aq.example.test", "must start with http:// or https://"),
        ("aq.example.test", "must start with http:// or https://"),
        ("https://aq.example.test:99999", "is not a valid URL"),
        ("https://aq_example.test", "has an invalid host name"),
    ],
)
def test_a_malformed_public_url_is_refused_without_echoing_it(configured, problem):
    link = resolve(public_url=configured)

    assert not link.url
    assert link.reason == "public_url_invalid"
    assert problem in link.detail
    assert "hunter2" not in link.detail + link.notice
    assert link.notice.startswith("Remote dashboard link unavailable (")


@pytest.mark.parametrize(
    "configured",
    ["http://localhost:8082", "http://127.0.0.1:8082", "http://[::1]:8082", "http://0.0.0.0:8082",
     "http://[::]:8082", "http://app.localhost:8082"],
)
def test_a_public_url_naming_this_machine_is_never_posted(configured):
    link = resolve(public_url=configured)

    assert not link.url
    assert link.reason == "public_url_local"
    assert "localhost" not in link.notice and "127.0.0.1" not in link.notice


def test_the_alias_fills_in_and_a_conflicting_pair_is_refused():
    alias_only = dashboard_server_config_from_raw(
        {"dashboard": {"public_url": "https://aq.tailnet.ts.net"}}
    )
    both_equal = dashboard_server_config_from_raw(
        {"dashboard": {"public_url": "https://aq.tailnet.ts.net/",
                       "server": {"public_url": "https://aq.tailnet.ts.net"}}}
    )
    conflict = dashboard_server_config_from_raw(
        {"dashboard": {"public_url": "https://old.tailnet.ts.net",
                       "server": {"public_url": "https://aq.tailnet.ts.net"}}}
    )

    def link_for(server):
        return asyncio.run(dashboard_link_base(
            DashboardLinkSettings.from_config(SimpleNamespace(dashboard_server=server)),
            probe=Probes(),
        ))

    assert alias_only.public_url == "https://aq.tailnet.ts.net"
    assert link_for(alias_only).url == "https://aq.tailnet.ts.net"
    assert link_for(alias_only).source == ALIAS_KEY
    assert link_for(both_equal).url == "https://aq.tailnet.ts.net"
    # The canonical key wins the parse, but a disagreement disables the link
    # rather than letting either one silently decide.
    assert conflict.public_url == "https://aq.tailnet.ts.net"
    refused = link_for(conflict)
    assert not refused.url and refused.reason == "public_url_conflict"
    assert "disagree" in refused.notice


def test_bad_link_settings_warn_at_load_but_never_stop_the_daemon(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "database": {"url": "postgresql+asyncpg://test:test@localhost/test"},
        "discord": {"bot_token": "t", "guild_id": "1"},
        "dashboard": {
            "public_url": "https://old.tailnet.ts.net",
            "server": {"public_url": "https://aq.tailnet.ts.net"},
        },
    }))

    config = load_config(str(path))

    assert config.dashboard_server.public_url == "https://aq.tailnet.ts.net"
    assert "conflicts with dashboard.public_url" in caplog.text
    warnings = [
        str(error) for error in DashboardServerConfig(public_url="https://x.test/path").validate()
    ]
    assert warnings == [
        (
            "[dashboard.server] public_url: must be an origin with no path; "
            "remote dashboard links are disabled"
        )
    ]


def test_a_disabled_server_has_no_link_even_with_a_public_url():
    link = resolve(enabled=False, public_url="https://aq.tailnet.ts.net")

    assert not link.url and link.reason == "server_disabled"
    assert "disabled" in link.notice


# ---------------------------------------------------------------------------
# 2-3. No public URL: a confirmed tailnet bind, or unavailable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "addresses", "url"),
    [
        ("100.99.1.2", ("100.99.1.2", "fd7a:115c:a1e0::55"), "http://100.99.1.2:8082"),
        ("fd7a:115c:a1e0::55", ("100.99.1.2", "fd7a:115c:a1e0::55"),
         "http://[fd7a:115c:a1e0::55]:8082"),
    ],
)
def test_a_direct_bind_on_this_nodes_tailnet_address_is_advertised(host, addresses, url):
    probe = Probes(tailnet(*addresses))

    link = resolve(probe, host=host, port=8082, tailscale_path="/opt/ts/tailscale")

    assert link.url == url
    assert link.reason == "tailnet_bind" and link.source == HOST_KEY
    assert link.edge_compatible is True
    assert probe.calls == ["/opt/ts/tailscale"]


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "0.0.0.0", "::"])
def test_a_loopback_or_wildcard_bind_is_a_notice_even_with_a_tailscale_identity(host):
    probe = Probes(tailnet("100.99.1.2"))

    link = resolve(probe, host=host, port=8082)

    # Identity is not reachability: 127.0.0.1:8082 is never rewritten to
    # 100.99.1.2:8082, and the CLI is not even asked.
    assert not link.url
    assert link.reason == "not_configured"
    assert "100.99.1.2" not in link.notice + link.detail
    assert CANONICAL_KEY in link.notice
    assert probe.calls == []


def test_a_lan_bind_is_not_advertised():
    probe = Probes(tailnet("100.99.1.2"))

    link = resolve(probe, host="192.168.1.20", port=8082)

    assert not link.url and link.reason == "not_configured"
    assert probe.calls == []


@pytest.mark.parametrize(
    "answer",
    [
        TailscaleProbe("missing", detail="the tailscale CLI is not on PATH"),
        TailscaleProbe("timeout", detail="tailscale ip did not answer within 2s"),
        tailnet("100.64.0.9"),
    ],
)
def test_an_unconfirmed_tailnet_bind_is_a_notice(answer):
    link = resolve(Probes(answer), host="100.99.1.2", port=8082)

    assert not link.url and link.reason == "tailnet_unconfirmed"
    assert link.notice.startswith("Remote dashboard link unavailable (")


def test_a_raising_probe_is_a_reason_not_a_crash():
    async def broken(_path: str) -> TailscaleProbe:
        raise RuntimeError("boom")

    link = asyncio.run(dashboard_link_base(
        DashboardLinkSettings(host="100.99.1.2"), probe=broken
    ))

    assert not link.url and link.reason == "tailnet_unconfirmed"
    assert "RuntimeError" in link.detail


def test_the_health_url_never_substitutes_for_a_dashboard_link():
    """health_check.base_url / port 8081 are the daemon's; it serves no dashboard pages."""
    config = app_config()
    config.health_check = HealthCheckConfig(base_url="https://queue.example.test", port=8081)

    link = asyncio.run(
        dashboard_link_base(DashboardLinkSettings.from_config(config), probe=Probes())
    )

    assert not link.url
    assert "queue.example.test" not in link.notice and ":8081" not in link.notice


# ---------------------------------------------------------------------------
# Edge compatibility and the report
# ---------------------------------------------------------------------------


def test_edge_compatibility_follows_the_trusted_origin_list():
    trusted = DashboardLinkSettings.from_config(
        app_config(public_url="https://aq.tailnet.ts.net/",
                   trusted=["https://AQ.tailnet.ts.net:443"])
    )
    untrusted = DashboardLinkSettings.from_config(app_config(public_url="https://aq.tailnet.ts.net"))

    assert asyncio.run(dashboard_link_base(trusted, probe=Probes())).edge_compatible is True
    assert asyncio.run(dashboard_link_base(untrusted, probe=Probes())).edge_compatible is False


def test_the_report_names_source_edge_and_cli_without_secrets():
    settings = DashboardLinkSettings.from_config(
        app_config(public_url="https://user:hunter2@aq.tailnet.ts.net")
    )

    report = asyncio.run(diagnose_dashboard_link(settings, probe=Probes()))

    assert report["available"] is False and report["reason"] == "public_url_invalid"
    assert report["source"] == CANONICAL_KEY
    assert report["tailscale"] == {"cli": "missing", "addresses": [], "detail": "not on PATH"}
    assert report["remote_reachability"] == REMOTE_REACHABILITY == "unverified"
    assert report["settings"]["public_url_set"] is True
    assert "hunter2" not in repr(report)
    assert {"url", "reason", "source", "checked_at", "generation"} <= set(report)


def test_origin_normalisation_matches_the_edges():
    from src.dashboard_server.settings import normalise_origin as edge_normalise

    samples = [
        "https://AQ.example.test", "https://aq.example.test:443", "http://aq.example.test:80",
        "http://[FD7A:115C:A1E0::1]:8082", "https://aq.example.test/", "https://u@aq.example.test",
        "ftp://aq.example.test", "https://aq.example.test/?q", "not a url", "http://:80",
    ]
    for sample in samples:
        assert normalise_origin(sample) == edge_normalise(sample), sample


def test_check_public_url_accepts_a_trailing_slash_only():
    assert check_public_url("https://aq.example.test/")[0] == "https://aq.example.test"
    assert check_public_url("https://aq.example.test/x")[1] == "public_url_invalid"


# ---------------------------------------------------------------------------
# The cached resolver
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_a_found_link_is_cached_five_minutes_and_a_missing_one_thirty_seconds():
    clock = Clock()
    config = app_config(host="100.99.1.2", port=8082)
    probe = Probes(TailscaleProbe("missing", detail="not yet"), tailnet("100.99.1.2"))
    resolver = DashboardLinkResolver(lambda: config, probe=probe, clock=clock, wall_clock=clock)

    async def scenario():
        first = await resolver.resolve()
        clock.now += NEGATIVE_TTL_SECONDS - 1
        cached_miss = await resolver.resolve()
        clock.now += 2
        recovered = await resolver.resolve()
        clock.now += POSITIVE_TTL_SECONDS - 1
        cached_hit = await resolver.resolve()
        return first, cached_miss, recovered, cached_hit

    first, cached_miss, recovered, cached_hit = asyncio.run(scenario())

    assert not first.url and cached_miss is first
    # Negative-cache recovery: tailscaled came up after the daemon started.
    assert recovered.url == "http://100.99.1.2:8082"
    assert cached_hit is recovered
    assert len(probe.calls) == 2
    assert recovered.checked_at == clock.now - (POSITIVE_TTL_SECONDS - 1)


def test_a_config_reload_invalidates_the_cache_at_once(caplog):
    clock = Clock()
    holder = {"config": app_config()}
    resolver = DashboardLinkResolver(
        lambda: holder["config"], probe=Probes(), clock=clock, wall_clock=clock
    )

    async def scenario():
        before = await resolver.resolve()
        holder["config"] = app_config(public_url="https://aq.tailnet.ts.net")
        after = await resolver.resolve()
        again = await resolver.resolve()
        return before, after, again

    with caplog.at_level("INFO", logger="src.remote_links"):
        before, after, again = asyncio.run(scenario())

    assert not before.url and before.generation == 1
    assert after.url == "https://aq.tailnet.ts.net" and after.generation == 2
    assert again is after
    assert resolver.current is after
    # One line per change of outcome, none for a cache hit.
    assert caplog.text.count("Dashboard link") == 2


# ---------------------------------------------------------------------------
# The real probe, against a stand-in executable
# ---------------------------------------------------------------------------


def _script(tmp_path: Path, body: str) -> str:
    path = tmp_path / "tailscale"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_the_probe_reads_tailnet_addresses_and_ignores_the_rest(tmp_path):
    path = _script(
        tmp_path,
        'test "$1" = ip || exit 9\nprintf "100.99.1.2\\n192.168.1.5\\nnot-an-ip\\n'
        'fd7a:115c:a1e0::55\\n"',
    )

    result = asyncio.run(probe_tailscale(path))

    assert result.status == "available"
    assert result.addresses == ("100.99.1.2", "fd7a:115c:a1e0::55")


def test_the_probe_reports_a_missing_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    missing = asyncio.run(probe_tailscale())
    not_executable = asyncio.run(probe_tailscale(str(tmp_path / "nope")))

    assert missing.status == "missing" and "PATH" in missing.detail
    assert not_executable.status == "missing" and "tailscale_path" in not_executable.detail


def test_the_probe_reports_a_failure_and_a_timeout(tmp_path):
    failing = asyncio.run(probe_tailscale(_script(tmp_path, "exit 1")))
    slow = asyncio.run(probe_tailscale(_script(tmp_path, "exec sleep 30"), deadline=0.3))

    assert failing.status == "error" and "exited 1" in failing.detail
    assert slow.status == "timeout" and "0.3s" in slow.detail


def test_the_probe_searches_path_when_no_explicit_path_is_set(tmp_path, monkeypatch):
    _script(tmp_path, 'echo 100.99.1.2')
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    assert asyncio.run(probe_tailscale()).addresses == ("100.99.1.2",)


# ---------------------------------------------------------------------------
# Every sender renders the same origin
# ---------------------------------------------------------------------------


class FakeTransport:
    def __init__(self) -> None:
        self.posts: list[str] = []

    async def post_root(self, *, channel_id: str, content: str):
        self.posts.append(content)


class FakeReviews:
    def __init__(self) -> None:
        self.rows = [{
            "id": "rev-1", "kind": "plan", "title": "t", "project_id": "p",
            "author_task_id": None, "current_revision": 1, "notified_revision": 0,
        }]

    async def reviews_pending_notification(self):
        return [row for row in self.rows if row["notified_revision"] < row["current_revision"]]

    async def mark_review_notified(self, review_id, revision):
        self.rows[0]["notified_revision"] = revision


FACTS = EscalationFacts(
    id="esc-1",
    project_id="project",
    state="needs_human",
    revision=0,
    severity="high",
    summary="blocked",
    investigation="checked the daemon",
    decision_requested="choose",
)
DIGEST_INPUTS = DigestInputs(
    window=DigestWindow(0, 3600),
    facts=(
        WorkFact(key="completion:1", kind="completed", category="work", project_id="project",
                 task_id="task", title="task", at=1),
    ),
)


def _render_everything(link) -> list[str]:
    transport = FakeTransport()
    notifier = ReviewNotifier(FakeReviews(), transport, "123", link_resolver=StaticLink(link))
    asyncio.run(notifier.tick())
    return [
        render_root(FACTS, mentions=MentionPolicy(), base_url=link.url, dedup_key="root",
                    dashboard_notice=link.notice),
        render_resolved_root(FACTS, base_url=link.url, dedup_key="resolved",
                             dashboard_notice=link.notice),
        render_thread_opener(FACTS, base_url=link.url, dedup_key="thread",
                             dashboard_notice=link.notice),
        render_resolution(FACTS, base_url=link.url, dedup_key="resolution",
                          dashboard_notice=link.notice),
        build_digest(DIGEST_INPUTS, dashboard_url=link.url, dashboard_notice=link.notice).text,
        *transport.posts,
    ]


class StaticLink:
    def __init__(self, link) -> None:
        self.link = link

    async def resolve(self):
        return self.link


def test_every_sender_renders_the_configured_origin():
    link = resolve(public_url="https://aq.tailnet.ts.net/")

    rendered = _render_everything(link)

    assert all("https://aq.tailnet.ts.net" in text for text in rendered)
    assert "https://aq.tailnet.ts.net/settings/messaging#escalation-reply-esc-1" in rendered[0]
    assert rendered[-1].endswith("https://aq.tailnet.ts.net/reviews/rev-1")
    assert not any(":8081" in text or "localhost" in text for text in rendered)


def test_every_sender_renders_the_notice_when_there_is_no_link():
    link = resolve(host="127.0.0.1", port=8082)

    rendered = _render_everything(link)

    assert all("Remote dashboard link unavailable" in text for text in rendered)
    assert not any("127.0.0.1" in text or "localhost" in text for text in rendered)


def test_a_static_link_without_a_base_carries_the_notice():
    link = asyncio.run(StaticDashboardLink().resolve())

    assert not link.url and "Remote dashboard link unavailable" in link.notice
    assert asyncio.run(StaticDashboardLink("https://aq.example.test/").resolve()).url == (
        "https://aq.example.test"
    )
