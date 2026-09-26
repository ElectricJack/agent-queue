"""Morning domain policy and summaries on the shared delivery outbox."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from urllib.parse import quote

from src.delivery.message import operation_marker
from src.digest.render import sanitise
from src.digest.schedule import schedule_for


def morning_policy(config) -> dict:
    policy = asdict(config.reports.morning)
    policy["destination"] = policy["destination"] or (
        schedule_for(config.discord).destination if config.discord.channel_id else ""
    )
    return policy


def visibility_matches(snapshot: dict, policy: dict) -> bool:
    return bool(
        policy["enabled"]
        and snapshot["destination"] == policy["destination"]
        and set(snapshot["project_ids"]) == set(policy["project_ids"])
        and snapshot.get("full_fleet_visibility", False) == policy["full_fleet_visibility"]
    )


def render_summary(row: dict, *, url: str, notice: str) -> str:
    """One bounded message; links, marker and coverage are server-owned."""
    report = row["report"]
    footer = f"{url.rstrip('/')}/reports/{quote(row['id'], safe='')}" if url else notice
    heading = f"Morning report · {row['local_date']}"
    coverage = report["coverage"]
    warnings = []
    if not coverage["complete"]:
        warnings.append("Partial coverage; see source gaps in the full report.")
    if coverage["window"].get("omitted_interval"):
        warnings.append("Lookback capped; some earlier activity is not covered.")
    tail = "\n".join([*warnings, footer])
    # The shared primitive uses this marker for every replay of the dedup key.
    delivery_id = "outbound-" + hashlib.sha256(f"morning:{row['id']}".encode()).hexdigest()[:32]
    marker = operation_marker(delivery_id, prefix="aq-out")
    budget = 1500 - len(f"{heading}\n\n{tail}\n{marker}")
    summary = sanitise(report["summary"])
    if budget < 1:
        raise ValueError("report link exceeds the delivery budget")
    summary = summary if len(summary) <= budget else summary[: budget - 1] + "…"
    return f"{heading}\n{summary}\n{tail}"


async def reconcile_morning_deliveries(db, config, *, now: float, links=None) -> None:
    """Recover finalized-but-unreserved reports without rebuilding or re-authoring."""
    policy = morning_policy(config)
    await db.reconcile_morning_visibility(policy=policy, now=now)
    if not policy["enabled"] or not policy["destination"]:
        return
    rows = await db.list_morning_delivery_candidates()
    if not rows:
        return
    url, notice = "", "Full report is available in the dashboard; no external link configured."
    if links is not None:
        link = await links.resolve()
        url, notice = link.url, link.unavailable_notice
    for row in rows:
        if visibility_matches(row["config_snapshot"], policy):
            await db.reserve_morning_delivery(
                row["id"],
                policy=policy,
                text=render_summary(row, url=url, notice=notice),
                now=now,
            )
