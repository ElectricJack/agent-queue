# Tailscale dashboard link not posting — preliminary spec (bug)

**Date:** 2026-09-24 · **Status:** preliminary — analysis and options, no decision taken
**Roadmap:** [operator feedback roadmap](2026-09-24-operator-feedback-roadmap.md)
**Related:** [mobile dashboard](2026-09-24-mobile-dashboard.md),
[dashboard performance and separation](2026-09-24-dashboard-performance-and-separation.md),
[Discord mention routing](2026-09-24-discord-mention-routing.md),
`docs/specs/dashboard-server.md` (§3.2–3.4 edge gates and exposure),
[Discord simplification](2026-09-08-discord-simplification-implementation.md) §7–§8,
[release roadmap](2026-09-06-release-roadmap.md) §10

## 1. The ask

> Bug: Tailscale dashboard link not posting.

Discord posts (hourly digest, escalation roots/threads) are supposed to carry a dashboard link
the operator can open from a phone over Tailscale. They do not produce a usable one.

## 2. What exists today

**Link resolution — `src/remote_links.py`.** `resolve_remote_link_base(base_url)` keeps a
non-loopback URL as is; for a loopback/wildcard host (`_LOOPBACK_HOSTS`, line 22) it runs
`tailscale ip -4`, `tailscale ip -6`, then `tailscale status --json` (`_run_tailscale`,
lines 36-49: `subprocess.run`, 2 s timeout, any error → `""`) and swaps in the first
CGNAT/`fd7a:115c:a1e0::/48` address or the MagicDNS name. With no identity it returns
`unavailable_notice = "Remote dashboard link unavailable (Tailscale has no usable identity;
open it on the daemon host)."` (lines 25, 129-133).

**Wiring — `src/main.py:318-357`.** Only when a Discord transport exists (cutover
`complete`) **and** `discord.channel_id` is set:

```python
local_base_url = (
    config.health_check.base_url or f"http://localhost:{config.health_check.port}"
)
remote_link_base = resolve_remote_link_base(local_base_url)   # once, at startup
```

`remote_link_base.url` / `.unavailable_notice` go to `EscalationDeliveryService` and
`DigestScheduleService`. Nothing logs the outcome; no doctor check covers it
(`grep -rn "remote_link\|tailscale" src/doctor` is empty).

**Rendering.** Escalations: `escalation_url` (`src/escalations/render.py:85-93`) →
`{base}/settings/messaging#escalation-reply-{id}`, or the notice when `base` is empty; used by
root, resolved root, thread opener and resolution. Digest: footer appends
`sanitise(dashboard_url)` or `sanitise(dashboard_notice)` (`src/digest/render.py:149-152`);
`sanitise` only strips `<@…>`/`<#…>` tokens, backticks and whitespace and zero-width-spaces
`@`, so it does not damage a normal URL. Transport sends plain content with explicit
`AllowedMentions` (`src/discord/escalation_transport.py:124-202`); no embed suppression.

**Other link producers.**
- Document reviews: `review_base_url = config.dashboard_server.public_url or
  f"http://{host}:{port}"` (`src/main.py:372-381`) → `ReviewNotifier` posts
  `{base}/reviews/{id}` (`src/reviews/notifier.py:17, 115`). **Never Tailscale-rewritten**;
  default `http://127.0.0.1:8082/...`.
- The daemon's `/dashboard` pointer, `_dashboard_hint_url` (`src/api/app.py:207-216`), uses
  `dashboard.server.host:port` only (wildcard → `127.0.0.1`), ignoring `public_url`.

**Config.** `health_check.base_url` / `port` (default 8081) — "externally-reachable URL used
to generate links" (`src/config.py:977-990`). `dashboard.server.public_url` — "Public
dashboard origin used in links sent outside the local machine" (`src/config.py:2834-2836`;
also accepted as `dashboard.public_url`, lines 2869-2870). Two keys claim the same job.

**Where things listen.** The daemon API (and MCP) serve on `mcp_server.host:port`, default
`127.0.0.1:8081` (`src/embedded_mcp.py:158-163`, `src/config.py:824-826`) and are
**API-only** — no SPA routes (`src/api/app.py:196-240`; only `/dashboard[/…]` answers, with a
307). The SPA, including `/settings/messaging` and `/reviews/:id`
(`dashboard/src/App.tsx:151-152`), is served by the dashboard server on
`dashboard.server.host:port`, default `127.0.0.1:8082`. Its edge refuses a `Host` that is not
loopback, the bind address, or a trusted origin's host (`misdirected_host`,
`src/dashboard_server/edge.py:118-131`); `docs/specs/dashboard-server.md` §3.4 recommends
`ssh -L` for remote use and warns that a LAN bind hands out the whole operator console.

**Tests.** `tests/test_remote_links.py:106-107` asserts the expected link is
`http://100.99.1.2:8081/settings/messaging#…` — the daemon port is baked into the contract.
`docs/guides/escalations.md:115` shows a digest footer of `http://localhost:8081`.

## 3. Gaps — hypotheses ranked

**Root-cause confidence.** From code alone it is certain that the link, *when* one is posted,
is unusable: it names the daemon API port, which is loopback-bound and has no dashboard pages,
and it ignores `dashboard.public_url`. Which of H1/H2 is the operator's literal symptom
("not posting") needs one observation on the host (§4 reproduction).

| # | Hypothesis | Likelihood | Evidence |
|---|---|---|---|
| H1 | **Wrong link source.** Digest/escalation base comes from `health_check` (daemon, :8081), not the dashboard server (:8082). An operator who set `dashboard.public_url` to a Tailscale URL (the key documented for this) sees no change in Discord. Even when rewriting works, `http://<ts-ip>:8081/settings/messaging` is refused (loopback bind) or 404s (API-only daemon). | **High (definite defect)** | `main.py:322-326`, `app.py:196-240`, `embedded_mcp.py:158-163`, `test_remote_links.py:106` |
| H2 | **Tailscale CLI not resolvable in the daemon's environment**, so every post carries the "Remote dashboard link unavailable…" notice instead of a URL. Causes: `tailscale` not on `PATH` (macOS GUI app without "Install CLI"; `_daemon_environment` in `src/cli/daemon.py:375-400` only appends `~/.local/bin` and pnpm), `tailscaled` not up when the daemon started (resolved **once**, never retried), or `tailscale ip` exceeding the 2 s timeout. | High | `remote_links.py:36-49, 129-133`; `main.py:326` |
| H3 | **Unreachable even with the right port.** The dashboard server binds `127.0.0.1` and the edge rejects a tailnet `Host`; a correct `http://<ts-ip>:8082/...` link still fails unless bound to the tailnet address or fronted by `tailscale serve` with the origin in `api_auth.trusted_dashboard_origins`. | High (for "link doesn't work") | `edge.py:118-131`, dashboard-server spec §3.4 |
| H4 | **Review posts use a loopback link.** If the missing link is on a document-review post, it is `http://127.0.0.1:8082/reviews/<id>` unless `public_url` is set. | Medium | `main.py:372-381`, `notifier.py:17` |
| H5 | **Nothing posts at all.** Cutover not `complete` or `discord.channel_id` empty → services not wired; `main.py:357-362` logs a warning. | Low (operator presumably sees digests) | `main.py:311-316` |
| H6 | **No post to carry it.** The link rides only on digests and escalations; a quiet window is suppressed by design (`src/digest/eligibility.py`), so "the link isn't posting" could mean "nothing posts". The dashboard's dry preview takes its own `dashboard_url` argument (`src/commands/digest_commands.py:141`) and can differ from real delivery. | Low | as cited |
| H7 | Discord does not auto-link a bare `http://100.x.y.z:port` URL. | Low, **unverified** | — |

## 4. Implementation options

**Reproduction plan (before choosing).**
1. On the operator host: read `~/.agent-queue/config.yaml` (or `get_config`) for
   `health_check.base_url`, `dashboard.public_url` / `dashboard.server.*`,
   `api_auth.trusted_dashboard_origins`, `discord.channel_id`.
2. Find the daemon PID; check its `PATH` (`ps eww <pid>` / `/proc/<pid>/environ`) and run
   `which tailscale` and `tailscale ip -4` under that `PATH`.
3. `python -c "from src.remote_links import resolve_remote_link_base as r; print(r('http://localhost:8081'))"`
   under the daemon's environment.
4. Read the text of the last digest / escalation root in the channel: notice (H2), `:8081` URL
   (H1), or no post (H5/H6).
5. From a tailnet phone: open the posted URL, then `http://<ts-ip>:8082/`, then the
   MagicDNS name — distinguishes H1 from H3.

### Option 1 — One link-base helper pointed at the dashboard server (fixes H1, H4)

`dashboard_link_base(config) -> RemoteLinkBase`: `dashboard.server.public_url` if set;
else legacy `health_check.base_url` if set (deprecated for links, warn once); else
`http://{dashboard.server.host}:{port}` passed through `resolve_remote_link_base`. Use it in
`main.py` for escalations, digest **and** reviews, and in the digest preview command. Fix
`test_remote_links.py` to expect `:8082`, add a wiring test, fix `escalations.md:115`.
*Pros.* Small, removes the two-keys ambiguity. *Cons.* Operators who set
`health_check.base_url` for links keep working only via the fallback. *Size:* S.

### Option 2 — Make resolution observable and self-healing (fixes H2)

Re-resolve lazily with a TTL (e.g. 5 min) instead of once at startup, off the event loop
(`asyncio.to_thread`; today three blocking `subprocess.run` calls can stall start-up ~6 s);
probe known CLI paths (`/Applications/Tailscale.app/Contents/MacOS/Tailscale`,
`/opt/homebrew/bin`, `/usr/local/bin`) when `tailscale` is not on `PATH`; log the resolved
base or the reason once per change; add `aq doctor --check dashboard.remote_link` (report the
base the next post will use and why). *Size:* S.

### Option 3 — Make the link reachable (fixes H3)

(3a) Document and doctor-check `tailscale serve --bg 8082` → `https://<host>.<tailnet>.ts.net`,
set `dashboard.public_url` to it and add it to `api_auth.trusted_dashboard_origins` (whether
`tailscale serve` preserves `Host` in a way the edge accepts is **unverified**); (3b) allow
binding the dashboard server to the tailnet IP, with the §3.4 exposure warning; (3c) setup
wizard step that detects Tailscale and offers 3a. *Size:* S (docs) – M (wizard + checks).

### Option 4 — Also post the link where the operator looks

A pinned channel message or `aq dashboard status` / startup log line with the remote URL.
*Size:* S. Only if the operator expects a standing link rather than per-post links (Q1).

## 5. Initial take

Provisional: **Options 1 + 2 now** (one small PR: correct source, lazy re-resolution,
logging, doctor check, corrected tests/docs), **Option 3a as documentation plus a doctor
check**, coordinated with the [mobile dashboard](2026-09-24-mobile-dashboard.md) spec, which
depends on remote reachability anyway. Option 3b/3c wait for that spec's auth decisions
(`require_session_token` is still not enforced — release roadmap §10).

## 6. Open questions

1. **What exactly does the operator see?** The "unavailable" notice, a `:8081` link that does
   not open, a loopback review link, or no post? Picks between H1–H6 and whether Option 2 is
   urgent.
2. **Which OS and Tailscale install?** macOS app (CLI often not on `PATH`), Homebrew, or
   Linux `tailscaled`? Decides the CLI probe list.
3. **Is `tailscale serve` (HTTPS MagicDNS) the supported remote path**, or a tailnet bind?
   Decides the preferred identity (hostname vs IP) and whether `public_url` becomes the
   recommended setting.
4. **Should `health_check.base_url` stop being a link source?** It also feeds the plan viewer
   (`src/api/app.py:120-122`, `src/api/health.py:121`), which the daemon still serves.
5. **Should the daemon ever run `tailscale serve` itself?** Convenient, but it is a network
   exposure change made by a service — likely a no.
6. **Standing link vs per-post links** (Option 4) — what does "the link" mean to the operator?

## 7. Dependencies and sequencing

- None blocking for Options 1–2.
- Option 3 overlaps the [mobile dashboard](2026-09-24-mobile-dashboard.md) and
  [dashboard performance and separation](2026-09-24-dashboard-performance-and-separation.md)
  specs (edge gates, auth, origins).
- [Mention routing](2026-09-24-discord-mention-routing.md) replies and
  [voice transcript](2026-09-24-discord-voice-transcripts.md) proposal summaries will embed the
  same link; land Option 1's helper first so they reuse it.

## 8. Non-goals

- Authentication for remote dashboard access (tracked with the mobile dashboard /
  `require_session_token`).
- LAN or public-internet exposure, tunnels other than Tailscale.
- Changing what the digest or escalation posts contain beyond the link.
