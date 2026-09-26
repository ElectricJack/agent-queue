"""The one dashboard origin externally posted links name (src/remote_links.py).

Vault spec ``2026-09-24-tailscale-dashboard-link.md`` §4-§5: a configured
``dashboard.server.public_url`` is used exactly and needs no Tailscale CLI; a
tailnet bind is advertised only when the CLI confirms it is this machine's;
everything else is an explicit notice.  ``health_check.base_url`` (the
daemon's port) is never a fallback, and a loopback bind is never rewritten to
a tailnet address.  Escalations, the digest, reviews and the digest preview
all render the resolver's answer.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from click.testing import CliRunner

from src.config import (
    ApiAuthConfig,
    ConfigValidationError,
    DashboardServerConfig,
    HealthCheckConfig,
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
    REASON_BIND_NOT_TAILNET,
    REASON_LOCAL_BIND,
    REASON_PUBLIC_URL,
    REASON_PUBLIC_URL_CONFLICT,
    REASON_PUBLIC_URL_INVALID,
    REASON_RESOLUTION_FAILED,
    REASON_SERVER_DISABLED,
    REASON_TAILNET_BIND,
    REASON_TAILNET_MISMATCH,
    REASON_TAILSCALE_UNAVAILABLE,
    DashboardLinkResolver,
    LinkSettings,
    TailnetProbe,
    dashboard_link_base,
    describe_link,
    edge_compatibility,
    local_dashboard_url,
    probe_tailnet,
    resolve_dashboard_link,
)

CHANNEL = "123456789012345678"


def config(*, auth: ApiAuthConfig | None = None, health=None, **server) -> SimpleNamespace:
    return SimpleNamespace(
        dashboard_server=DashboardServerConfig(**server),
        api_auth=auth or ApiAuthConfig(),
        health_check=health or HealthCheckConfig(),
    )


def settings(**server) -> LinkSettings:
    return LinkSettings.from_config(config(**server))


async def no_probe(_settings: LinkSettings) -> TailnetProbe:
    raise AssertionError("the Tailscale CLI must not be consulted here")


def fixed_probe(*addresses: str, error: str = ""):
    calls: list[LinkSettings] = []

    async def probe(value: LinkSettings) -> TailnetProbe:
        calls.append(value)
        return TailnetProbe(addresses=tuple(addresses), error=error)

    probe.calls = calls  # type: ignore[attr-defined]
    return probe


# ---------------------------------------------------------------------------
# Rule 1: an explicit public URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("public_url", "expected"),
    [
        ("https://queue.tail1234.ts.net", "https://queue.tail1234.ts.net"),
        ("https://Queue.Tail1234.TS.net/", "https://queue.tail1234.ts.net"),
        ("https://queue.tail1234.ts.net:443/", "https://queue.tail1234.ts.net"),
        ("http://queue.example.test:80", "http://queue.example.test"),
        ("https://queue.example.test:8443", "https://queue.example.test:8443"),
        ("http://100.99.1.2:8082", "http://100.99.1.2:8082"),
        ("https://[fd7a:115c:a1e0::55]:8443/", "https://[fd7a:115c:a1e0::55]:8443"),
    ],
)
async def test_a_public_url_is_used_exactly_normalised_and_needs_no_tailscale_cli(
    public_url, expected
):
    link = await resolve_dashboard_link(settings(public_url=public_url), probe=no_probe)

    assert link.url == expected
    assert (link.reason, link.source) == (REASON_PUBLIC_URL, "dashboard.server.public_url")
    assert link.unavailable_notice == ""
    assert link.to_dict()["remote_reachability"] == "unverified"


@pytest.mark.parametrize(
    ("public_url", "fragment"),
    [
        ("https://user:hunter2@queue.example.test", "credentials"),
        ("https://queue.example.test/?token=hunter2", "query or fragment"),
        ("https://queue.example.test/#hunter2", "query or fragment"),
        ("https://queue.example.test/dashboard", "without a path"),
        ("ftp://queue.example.test", "http(s) URL"),
        ("queue.example.test", "http(s) URL"),
        ("https://queue.example.test:99999", "not a valid URL"),
        ("https://*.example.test", "wildcards"),
        ("http://localhost:8082", "loopback or wildcard"),
        ("http://127.0.0.1:8082", "loopback or wildcard"),
        ("http://[::1]:8082", "loopback or wildcard"),
        ("http://0.0.0.0:8082", "loopback or wildcard"),
        ("http://[::ffff:127.0.0.1]:8082", "loopback or wildcard"),
    ],
)
def test_a_forbidden_public_url_is_refused_without_echoing_it(public_url, fragment):
    link = dashboard_link_base(settings(public_url=public_url))

    assert link.url == ""
    assert link.reason == REASON_PUBLIC_URL_INVALID
    assert fragment in link.detail
    assert "hunter2" not in link.unavailable_notice
    assert link.unavailable_notice.startswith("Remote dashboard link unavailable (")


def test_conflicting_public_url_aliases_are_rejected_by_the_loader(tmp_path):
    path = tmp_path / "config.yaml"
    base = {
        "database": {"url": "postgresql+asyncpg://test:test@localhost/test"},
        "discord": {"bot_token": "t", "guild_id": "1"},
    }
    path.write_text(yaml.dump({
        **base,
        "dashboard": {
            "public_url": "https://a.example.test",
            "server": {"public_url": "https://b.example.test"},
        },
    }))
    with pytest.raises(ConfigValidationError, match="name different origins"):
        load_config(str(path))

    # Equal as origins is agreement, and the alias alone still fills in.
    path.write_text(yaml.dump({
        **base,
        "dashboard": {
            "public_url": "https://a.example.test/",
            "server": {"public_url": "https://A.example.test"},
        },
    }))
    assert load_config(str(path)).dashboard_server.public_url == "https://A.example.test"
    path.write_text(yaml.dump({**base, "dashboard": {"public_url": "https://a.example.test"}}))
    assert load_config(str(path)).dashboard_server.public_url == "https://a.example.test"


def test_a_conflict_read_from_raw_yaml_is_an_unavailable_link():
    raw = {
        "dashboard": {
            "public_url": "https://a.example.test",
            "server": {"public_url": "https://b.example.test"},
        }
    }
    link = dashboard_link_base(LinkSettings.from_raw(raw))

    assert link.url == "" and link.reason == REASON_PUBLIC_URL_CONFLICT


def test_an_invalid_public_url_warns_but_never_keeps_the_daemon_down(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump({
        "database": {"url": "postgresql+asyncpg://test:test@localhost/test"},
        "discord": {"bot_token": "t", "guild_id": "1"},
        "dashboard": {"server": {"public_url": "https://queue.example.test/dashboard"}},
    }))

    loaded = load_config(str(path))

    assert loaded.dashboard_server.public_url == "https://queue.example.test/dashboard"
    assert "external dashboard links are disabled" in caplog.text


# ---------------------------------------------------------------------------
# Rules 2 and 3: the bind address, and never the health port
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "0.0.0.0", "::"])
async def test_a_local_bind_is_a_notice_even_with_a_valid_tailscale_identity(host):
    probe = fixed_probe("100.99.1.2", "fd7a:115c:a1e0::55")

    link = await resolve_dashboard_link(settings(host=host), probe=probe)

    assert link.url == ""
    assert link.reason == REASON_LOCAL_BIND
    assert probe.calls == []  # an identity would prove nothing about a loopback port
    assert "100.99.1.2" not in link.unavailable_notice


async def test_health_check_base_url_is_never_a_dashboard_fallback():
    daemon_side = config(
        health=HealthCheckConfig(enabled=True, port=8081, base_url="http://100.99.1.2:8081"),
    )

    link = await resolve_dashboard_link(LinkSettings.from_config(daemon_side), probe=no_probe)

    assert link.url == ""
    assert "8081" not in link.unavailable_notice


async def test_a_tailnet_bind_confirmed_by_the_cli_is_advertised_directly():
    ipv4 = await resolve_dashboard_link(
        settings(host="100.99.1.2", port=8082), probe=fixed_probe("100.99.1.2")
    )
    ipv6 = await resolve_dashboard_link(
        settings(host="fd7a:115c:a1e0::55", port=8082),
        probe=fixed_probe("100.99.1.2", "fd7a:115c:a1e0::55"),
    )

    assert (ipv4.url, ipv4.reason, ipv4.source) == (
        "http://100.99.1.2:8082", REASON_TAILNET_BIND, "dashboard.server.host",
    )
    assert ipv6.url == "http://[fd7a:115c:a1e0::55]:8082"
    # The edge answers for its own bind address, same-origin included.
    edge = edge_compatibility(ipv4, settings(host="100.99.1.2", port=8082))
    assert edge["host_allowed"] and edge["origin_allowed"]


async def test_an_unconfirmed_or_foreign_tailnet_bind_is_a_notice():
    missing = await resolve_dashboard_link(
        settings(host="100.99.1.2"), probe=fixed_probe(error="tailscale CLI missing")
    )
    other = await resolve_dashboard_link(
        settings(host="100.99.1.2"), probe=fixed_probe("100.99.9.9")
    )
    lan = await resolve_dashboard_link(settings(host="192.168.1.20"), probe=no_probe)

    assert (missing.url, missing.reason) == ("", REASON_TAILSCALE_UNAVAILABLE)
    assert (other.url, other.reason) == ("", REASON_TAILNET_MISMATCH)
    assert (lan.url, lan.reason) == ("", REASON_BIND_NOT_TAILNET)


def test_a_disabled_server_is_a_notice_and_has_no_local_url():
    disabled = settings(enabled=False, public_url="https://queue.example.test")

    assert dashboard_link_base(disabled).reason == REASON_SERVER_DISABLED
    assert dashboard_link_base(disabled).unavailable_notice
    assert local_dashboard_url(disabled) is None
    assert local_dashboard_url(settings(host="0.0.0.0", port=9090)) == "http://127.0.0.1:9090/"
    assert local_dashboard_url(settings(host="::1", port=9090)) == "http://[::1]:9090/"


def test_edge_compatibility_needs_the_public_origin_to_be_trusted():
    untrusted = settings(public_url="https://queue.tail1234.ts.net")
    trusted = LinkSettings.from_config(config(
        public_url="https://queue.tail1234.ts.net",
        auth=ApiAuthConfig(trusted_dashboard_origins=["https://queue.tail1234.ts.net"]),
    ))

    assert edge_compatibility(dashboard_link_base(untrusted), untrusted) == {
        "host_allowed": False, "origin_allowed": False, "trusted": False,
    }
    assert edge_compatibility(dashboard_link_base(trusted), trusted) == {
        "host_allowed": True, "origin_allowed": True, "trusted": True,
    }


# ---------------------------------------------------------------------------
# The real probe: PATH or tailscale_path, bounded in time and output
# ---------------------------------------------------------------------------


def _fake_cli(tmp_path: Path, body: str) -> str:
    path = tmp_path / "tailscale"
    path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


async def test_the_probe_runs_the_configured_cli_and_keeps_only_tailnet_addresses(tmp_path):
    cli = _fake_cli(
        tmp_path,
        "import sys\nassert sys.argv[1:] == ['ip']\n"
        "print('100.99.1.2\\nfd7a:115c:a1e0::55\\n192.168.1.10\\nnot-an-ip')",
    )

    probe = await probe_tailnet(settings(tailscale_path=cli))

    assert probe == TailnetProbe(addresses=("100.99.1.2", "fd7a:115c:a1e0::55"))


async def test_a_missing_failing_or_silent_cli_is_an_error_not_an_address(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    on_path = await probe_tailnet(settings())
    not_executable = await probe_tailnet(settings(tailscale_path=str(tmp_path / "nope")))
    failing = await probe_tailnet(
        settings(tailscale_path=_fake_cli(tmp_path, "raise SystemExit(3)"))
    )

    assert "not on the daemon's PATH" in on_path.error
    assert "not an executable file" in not_executable.error
    assert "status 3" in failing.error
    assert not (on_path.addresses or not_executable.addresses or failing.addresses)


async def test_a_hung_cli_is_killed_at_the_deadline(tmp_path):
    cli = _fake_cli(tmp_path, "import time\ntime.sleep(30)")

    started = time.monotonic()
    probe = await probe_tailnet(settings(tailscale_path=cli), deadline=0.5)

    assert "did not answer within 0.5s" in probe.error
    assert time.monotonic() - started < 5


async def test_unbounded_cli_output_is_refused(tmp_path):
    cli = _fake_cli(tmp_path, "import sys\nsys.stdout.write('100.99.1.2\\n' * 100000)")

    probe = await probe_tailnet(settings(tailscale_path=cli))

    assert probe.addresses == () and "more output than expected" in probe.error


# ---------------------------------------------------------------------------
# The cached resolver
# ---------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


async def test_a_negative_result_is_retried_after_thirty_seconds_and_recovers():
    live = config(host="100.99.1.2", port=8082)
    answers = [TailnetProbe(error="tailscaled is not up yet"), TailnetProbe(("100.99.1.2",))]
    calls: list[int] = []

    async def probe(_settings):
        calls.append(1)
        return answers[min(len(calls), len(answers)) - 1]

    clock = Clock()
    resolver = DashboardLinkResolver(lambda: live, probe=probe, clock=clock)

    first = await resolver.resolve()
    clock.now += 29
    cached = await resolver.resolve()
    clock.now += 2
    recovered = await resolver.resolve()

    assert first.reason == REASON_TAILSCALE_UNAVAILABLE and cached == first
    assert recovered.url == "http://100.99.1.2:8082"
    assert len(calls) == 2
    assert recovered.generation == first.generation  # same settings, same generation


async def test_a_positive_result_is_reused_for_five_minutes():
    live = config(host="100.99.1.2", port=8082)
    probe = fixed_probe("100.99.1.2")
    clock = Clock()
    resolver = DashboardLinkResolver(lambda: live, probe=probe, clock=clock)

    await resolver.resolve()
    clock.now += 299
    await resolver.resolve()
    assert len(probe.calls) == 1
    clock.now += 2
    await resolver.resolve()
    assert len(probe.calls) == 2


async def test_a_config_reload_invalidates_at_once_with_a_new_generation():
    live = config()
    resolver = DashboardLinkResolver(lambda: live, probe=no_probe, clock=Clock())

    before = await resolver.resolve()
    # ConfigWatcher applies a hot-reloadable section by replacing it in place.
    live.dashboard_server = DashboardServerConfig(public_url="https://queue.tail1234.ts.net")
    after = await resolver.resolve()

    assert before.url == "" and after.url == "https://queue.tail1234.ts.net"
    assert after.generation == before.generation + 1
    assert after.checked_at == 1_000.0


async def test_the_resolver_never_raises(caplog):
    async def exploding(_settings):
        raise RuntimeError("boom")

    resolver = DashboardLinkResolver(lambda: config(host="100.99.1.2"), probe=exploding)
    broken = DashboardLinkResolver(lambda: (_ for _ in ()).throw(RuntimeError("no config")))

    assert (await resolver.resolve()).reason == REASON_RESOLUTION_FAILED
    assert (await broken.resolve()).reason == REASON_RESOLUTION_FAILED
    assert (await broken.resolve()).unavailable_notice


async def test_each_new_outcome_is_logged_once(caplog):
    live = config()
    resolver = DashboardLinkResolver(lambda: live, probe=no_probe, clock=Clock())
    caplog.set_level("INFO", logger="src.remote_links")

    await resolver.resolve()
    await resolver.resolve()
    live.dashboard_server = DashboardServerConfig(public_url="https://queue.tail1234.ts.net")
    await resolver.resolve()

    messages = [r.getMessage() for r in caplog.records if r.name == "src.remote_links"]
    assert len(messages) == 2
    assert "unavailable (reason local_bind)" in messages[0]
    assert "https://queue.tail1234.ts.net" in messages[1] and "unverified" in messages[1]


# ---------------------------------------------------------------------------
# Every sender renders the same origin
# ---------------------------------------------------------------------------


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
INPUTS = DigestInputs(
    window=DigestWindow(0, 3600),
    facts=(
        WorkFact(
            key="completion:1", kind="completed", category="work", project_id="project",
            task_id="task", title="task", at=1,
        ),
    ),
)


class DigestDb:
    async def list_digest_windows(self, **_kwargs):
        return []

    async def list_escalations(self, **_kwargs):
        return []

    async def collect_digest_activity(self, *_args, **_kwargs):
        return INPUTS


class ReviewDb:
    def __init__(self) -> None:
        self.reviews = [{
            "id": "rev-1", "kind": "plan", "title": "t", "project_id": "p",
            "current_revision": 1, "notified_revision": 0,
        }]

    async def reviews_pending_notification(self):
        return [r for r in self.reviews if r["notified_revision"] < r["current_revision"]]

    async def mark_review_notified(self, review_id, revision):
        self.reviews[0]["notified_revision"] = revision


class Posts:
    def __init__(self) -> None:
        self.contents: list[str] = []

    async def post_root(self, *, channel_id, content):
        self.contents.append(content)


async def _rendered_by_every_sender(resolver: DashboardLinkResolver) -> list[str]:
    from src.config import DiscordConfig
    from src.digest import DigestScheduleService
    from src.digest.schedule import schedule_for
    from src.escalations import EscalationDeliveryService
    from src.reviews.notifier import ReviewNotifier

    discord = DiscordConfig(channel_id=CHANNEL)
    escalations = EscalationDeliveryService(
        None, None, config=discord, lease_owner="t", base_url="http://localhost:8081",
        links=resolver,
    )
    base_url, notice = await escalations._dashboard()
    texts = [
        render_root(FACTS, mentions=MentionPolicy(), base_url=base_url, dedup_key="r",
                    dashboard_notice=notice),
        render_resolved_root(FACTS, base_url=base_url, dedup_key="x", dashboard_notice=notice),
        render_thread_opener(FACTS, base_url=base_url, dedup_key="o", dashboard_notice=notice),
        render_resolution(FACTS, base_url=base_url, dedup_key="s", dashboard_notice=notice),
    ]
    digest = DigestScheduleService(
        DigestDb(), None, config=discord, lease_owner="t", base_url="http://localhost:8081",
        links=resolver,
    )
    result = await digest._evaluate_window(
        schedule_for(discord), DigestWindow(0, 3600), now=3600.0
    )
    texts.append(result.text)
    posts = Posts()
    await ReviewNotifier(ReviewDb(), posts, CHANNEL, links=resolver).tick()
    texts.extend(posts.contents)
    return texts


async def test_every_sender_renders_the_configured_public_origin():
    resolver = DashboardLinkResolver(
        lambda: config(public_url="https://queue.tail1234.ts.net/"), probe=no_probe
    )

    texts = await _rendered_by_every_sender(resolver)

    origin = "https://queue.tail1234.ts.net"
    assert all(f"{origin}/settings/messaging#escalation-reply-esc-1" in t for t in texts[:4])
    assert texts[4].rstrip().endswith(origin)
    assert texts[5].endswith(f"{origin}/reviews/rev-1")
    for text in texts:
        assert "8081" not in text and "localhost" not in text and "127.0.0.1" not in text


async def test_every_sender_renders_the_same_notice_without_an_origin():
    resolver = DashboardLinkResolver(lambda: config(), probe=no_probe)
    notice = (await resolver.resolve()).unavailable_notice

    texts = await _rendered_by_every_sender(resolver)

    assert notice and all(notice in text for text in texts)
    for text in texts:
        assert "8081" not in text and "8082" not in text and "localhost" not in text


def test_a_review_notifier_without_an_origin_never_posts_a_loopback_link():
    import asyncio

    from src.reviews.notifier import ReviewNotifier

    posts = Posts()
    asyncio.run(ReviewNotifier(ReviewDb(), posts, CHANNEL).tick())

    assert "127.0.0.1" not in posts.contents[0]
    assert "Remote dashboard link unavailable" in posts.contents[0]


def test_a_digest_notice_survives_sanitising():
    notice = dashboard_link_base(settings()).unavailable_notice

    assert notice in build_digest(INPUTS, dashboard_notice=notice).text


# ---------------------------------------------------------------------------
# Diagnostics: the shared report and `aq dashboard link`
# ---------------------------------------------------------------------------


def test_the_report_names_only_allowlisted_facts(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    value = settings(public_url="https://queue.tail1234.ts.net")

    report = describe_link(
        value, dashboard_link_base(value), server_health="running",
        discord_channel_configured=True,
    )

    assert report["url"] == "https://queue.tail1234.ts.net"
    assert report["source"] == "dashboard.server.public_url"
    assert report["remote_reachability"] == "unverified"
    assert report["edge"]["trusted"] is False
    assert report["server"] == {
        "enabled": True, "local_url": "http://127.0.0.1:8082/", "health": "running",
    }
    assert report["tailscale_cli"]["consulted"] is False
    assert set(report) == {
        "url", "reason", "detail", "source", "checked_at", "generation", "unavailable_notice",
        "remote_reachability", "edge", "server", "tailscale_cli", "discord_channel_configured",
    }


@pytest.fixture
def cli_config(tmp_path, monkeypatch):
    import src.cli.daemon as daemon_mod
    from src.dashboard_server.process import ServerStatus

    for name in ("AQ_SESSION_ID", "AQ_SESSION_KIND", "AQ_DB_SCOPE"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(daemon_mod, "CONFIG_PATH", str(path))
    monkeypatch.setattr(
        "src.cli.dashboard.dashboard_server_status",
        lambda api_url=None: ServerStatus("stopped", enabled=True),
    )
    return path


def test_aq_dashboard_link_json_reports_the_configured_origin(cli_config):
    from src.cli.app import cli

    cli_config.write_text(yaml.dump({
        "dashboard": {"server": {"public_url": "https://queue.tail1234.ts.net"}},
        "api_auth": {"trusted_dashboard_origins": ["https://queue.tail1234.ts.net"]},
        "discord": {"channel_id": CHANNEL},
        "health_check": {"base_url": "http://100.99.1.2:8081"},
    }))

    result = CliRunner().invoke(cli, ["dashboard", "link", "--json"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["url"] == "https://queue.tail1234.ts.net"
    assert data["edge"] == {"host_allowed": True, "origin_allowed": True, "trusted": True}
    assert data["server"]["health"] == "stopped"
    assert data["discord_channel_configured"] is True
    assert "8081" not in result.output


def test_aq_dashboard_link_explains_a_missing_origin(cli_config):
    from src.cli.app import cli

    cli_config.write_text(yaml.dump({"dashboard": {"server": {"host": "127.0.0.1"}}}))

    result = CliRunner().invoke(cli, ["dashboard", "link"])

    assert result.exit_code == 0, result.output
    assert "unavailable" in result.output
    assert "dashboard.server.public_url is not set" in " ".join(result.output.split())
    assert "unverified" in result.output


def test_aq_dashboard_link_refuses_unreadable_yaml(cli_config):
    from src.cli.app import cli

    cli_config.write_text("dashboard: [unclosed\n")

    result = CliRunner().invoke(cli, ["dashboard", "link", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.output)["error"]["code"] == "config_unreadable"


def test_the_module_needs_nothing_beyond_the_standard_library_at_import():
    """The daemon, doctor and CLI all import it; it must stay a leaf."""
    import subprocess

    code = (
        "import sys, src\n"
        "before = set(sys.modules)\n"
        "import src.remote_links\n"
        "added = set(sys.modules) - before - {'src.remote_links'}\n"
        "print(','.join(sorted(m for m in added if m.startswith('src'))))\n"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AQ_", "AGENT_QUEUE_"))}
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, timeout=60, check=True,
    )

    assert result.stdout.strip() == ""
