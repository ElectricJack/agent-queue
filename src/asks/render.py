"""Complete meaning for simple cards; longer decisions lead to their full packet."""

from dataclasses import dataclass
from urllib.parse import quote

from src.discord.render_budget import cut, link_line
from src.digest.render import sanitise
from src.escalations.transport import _units


@dataclass(frozen=True)
class AskCard:
    text: str
    choices: tuple[dict, ...] = ()


def render(ask, *, base_url, notice=""):
    context = ask["context"]
    document = ask["source_kind"] in {"review", "standalone_spec"}
    pending = ask["decision_state"] == "pending" and ask["application_state"] == "not_started"
    title = sanitise(context.get("title", "Request needs context"))
    by = sanitise(context.get("requested_by", {}).get("label", "AQ"))
    project = sanitise(ask["project_id"])
    state = (
        "Waiting for your decision"
        if pending
        else f"{ask['decision_state'].replace('_', ' ')} · {ask['application_state']}"
    )
    choices = tuple(context.get("choices", ()))
    lines = [
        f"{'📄 Read and discuss' if document else '❓'} {title}",
        f"Requested by {by} · {project}",
        sanitise(context.get("why", "")),
    ]
    effects = [f"{sanitise(c['label'])}: {sanitise(c['effect'])}" for c in choices]
    complete = "\n".join([*lines, *effects, sanitise(context.get("waiting_effect", "")), state])
    simple = (
        pending
        and not document
        and ask["context_state"] == "complete"
        and len(choices) <= 5
        and _units(complete) <= 600
        and all(c.get("application") == "immediate" and len(c["label"]) <= 80 for c in choices)
    )
    link = link_line(base_url, f"/focus/asks/{quote(ask['id'], safe='')}", notice=notice)
    if not base_url:
        return AskCard(
            f"⚠️ Delivery configuration needs attention: {notice or 'Set dashboard.server.public_url'}"
        )
    if simple:
        return AskCard(f"{complete}\n{link}", choices)
    # No partially shown effect is actionable: the packet link is the decision surface.
    summary = "\n".join(
        [
            cut(lines[0], 220),
            cut(lines[1], 100),
            cut(lines[2], 130),
            "Read the full question, effects and waiting work before deciding.",
            state,
        ]
    )
    return AskCard(f"{summary}\n{link}")
