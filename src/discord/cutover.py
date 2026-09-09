"""One-way migration from legacy Discord interactions to escalations.

The simplified adapter never starts the legacy notification consumer. Before
it accepts an escalation-thread reply, this coordinator inventories durable
questions, human gates and historical task-thread IDs, creates the missing
transport-neutral escalation identities, and adopts bot-owned cards that are
already in the selected shared channel. Every write has a durable unique key,
so a crash or restart can repeat the whole pass safely.

Historical Discord content is only made inert (``view=None`` by the adapter).
Nothing here deletes a channel, post or thread, and no task/gate/session
mutation is available from this module.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

from src.database.queries.escalation_queries import TERMINAL_ESCALATION_STATES

_MACHINE_GATES = frozenset({"routing", "task", "pr-merged", "ci-run", "timer", "event"})


@dataclass(frozen=True)
class LegacyBinding:
    """A bot-owned legacy card discovered by durable ID or history scan."""

    source_kind: str
    source_identity: str
    channel_id: str
    message_id: str


@dataclass
class CutoverReport:
    """Operator-visible result of one idempotent cutover pass."""

    status: str = "complete"
    channel_id: str = ""
    migrated_questions: int = 0
    migrated_gates: int = 0
    accepted_answers_preserved: int = 0
    adopted_roots: int = 0
    retired_task_threads: int = 0
    inert_messages: int = 0
    conflicts: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        if self.status != "complete":
            detail = "; ".join(self.conflicts) or "explicit channel selection required"
            return f"Discord cutover {self.status}: {detail}"
        return (
            "Discord cutover complete: "
            f"{self.migrated_questions} question(s), {self.migrated_gates} gate(s), "
            f"{self.accepted_answers_preserved} accepted answer(s), "
            f"{self.adopted_roots} adopted root(s), and "
            f"{self.retired_task_threads} historical task thread(s) made routing-inert."
        )


def _actor(value: Any) -> str:
    """Retain an old answer's actor without upgrading unverified provenance."""
    actor = str(value or "legacy:unknown")
    if actor.startswith("human:"):
        return actor
    if actor.startswith(("discord:", "dashboard:")):
        return f"human:{actor}"
    return actor


class LegacyConversationMigrator:
    """Migrate durable pending conversations exactly once."""

    def __init__(self, db: Any, *, clock: Any = time.time) -> None:
        self.db = db
        self._clock = clock

    async def run(
        self,
        *,
        channel_id: str,
        bindings: Iterable[LegacyBinding] = (),
        inert_messages: int = 0,
    ) -> CutoverReport:
        report = CutoverReport(channel_id=channel_id, inert_messages=inert_messages)
        by_source = {
            (binding.source_kind, binding.source_identity): binding for binding in bindings
        }
        now = float(self._clock())

        for question in await self.db.list_agent_questions(pending_only=True):
            state = str(question.get("state") or "")
            if not question.get("requires_human") and state not in ("human", "answered"):
                continue
            incident, created = await self._question(question, now=now)
            report.migrated_questions += int(created)
            if state == "answered":
                if await self._preserve_accepted_answer(question, incident, now=now):
                    report.accepted_answers_preserved += 1
                # This conversation was already completed on the old surface.
                # Preserve its evidence, but do not create a new thread or any
                # delivery work merely because the rollout observed it.
                continue
            if await self._adopt(
                incident,
                by_source.get(("question", str(question["id"]))),
                channel_id=channel_id,
                now=now,
            ):
                report.adopted_roots += 1

        for gate in await self.db.list_gates(status="open"):
            if str(gate.get("gate_type") or "") in _MACHINE_GATES:
                continue
            incident, created = await self._gate(gate, now=now)
            report.migrated_gates += int(created)
            if await self._adopt(
                incident,
                by_source.get(("gate", str(gate["id"]))),
                channel_id=channel_id,
                now=now,
            ):
                report.adopted_roots += 1

        report.retired_task_threads = sum(
            1 for task in await self.db.list_tasks() if getattr(task, "discord_thread_id", None)
        )
        return report

    async def _question(
        self, question: Mapping[str, Any], *, now: float
    ) -> tuple[dict[str, Any], bool]:
        task = await self.db.get_task(str(question["task_id"]))
        task_status = getattr(getattr(task, "status", None), "value", None)
        return await self.db.create_escalation(
            id=f"escalation-{question['id']}",
            project_id=str(question["project_id"]),
            task_id=str(question["task_id"]),
            source_kind="question",
            source_identity=str(question["id"]),
            incident_key=f"question:{question['id']}",
            supervisor_owner=f"supervisor-{question['project_id']}",
            task_title=getattr(task, "title", None),
            task_status=task_status,
            summary=f"Worker question on task {question['task_id']}",
            investigation=str(question.get("reason") or "Migrated from legacy Discord."),
            decision_requested=str(question.get("question") or "Human answer required."),
            choices=None,
            severity="medium",
            now=now,
        )

    async def _gate(
        self, gate: Mapping[str, Any], *, now: float
    ) -> tuple[dict[str, Any], bool]:
        return await self.db.create_escalation(
            id=f"escalation-gate-{gate['id']}",
            project_id=str(gate["project_id"]),
            task_id=None,
            source_kind="gate",
            source_identity=str(gate["id"]),
            incident_key=f"gate:{gate['id']}",
            supervisor_owner=f"supervisor-{gate['project_id']}",
            task_title=None,
            task_status=None,
            summary=str(gate.get("title") or f"Human gate {gate['id']}"),
            investigation="Migrated from a pending legacy Discord gate card.",
            decision_requested=str(gate.get("question") or "Human decision required."),
            choices=None,
            severity="medium",
            now=now,
        )

    async def _preserve_accepted_answer(
        self, question: Mapping[str, Any], incident: Mapping[str, Any], *, now: float
    ) -> bool:
        """Record accepted evidence without queuing or delivering it again."""
        answer = str(question.get("answer") or "").strip()
        if not answer:
            return False
        _, created = await self.db.append_escalation_message(
            str(incident["id"]),
            direction="inbound",
            transport="discord-legacy",
            verified_actor=_actor(question.get("answered_by")),
            text=answer,
            external_message_id=f"accepted-question:{question['id']}",
            received_at=float(question.get("updated_at") or now),
            message_id=f"legacy-answer-{question['id']}",
        )
        current = await self.db.get_escalation(str(incident["id"]))
        if current and current["state"] not in TERMINAL_ESCALATION_STATES:
            await self.db.resolve_legacy_accepted_escalation(
                str(incident["id"]),
                expected_revision=int(current["revision"]),
                terminal_evidence={
                    "question_id": str(question["id"]),
                    "answered_by": str(question.get("answered_by") or ""),
                    "question_state": "answered",
                    "redelivery": False,
                },
                now=now,
            )
        return created

    async def _adopt(
        self,
        incident: Mapping[str, Any],
        binding: LegacyBinding | None,
        *,
        channel_id: str,
        now: float,
    ) -> bool:
        if binding is None or binding.channel_id != channel_id:
            return False
        _row, created = await self.db.seed_legacy_escalation_root(
            str(incident["id"]),
            channel_id=binding.channel_id,
            root_message_id=binding.message_id,
            available_at=now,
        )
        return created


def _gate_identity(message: Any) -> str | None:
    """Read a legacy gate ID from a bot-authored embed without trusting text."""
    for embed in getattr(message, "embeds", ()) or ():
        for field in getattr(embed, "fields", ()) or ():
            name = str(getattr(field, "name", "") or "").strip().lower()
            if name != "gate id":
                continue
            value = str(getattr(field, "value", "") or "").strip().strip("`")
            if value:
                return value
    return None


async def _resolve_channel(bot: Any, channel_id: str) -> Any | None:
    if not channel_id.isdigit():
        return None
    channel = bot.get_channel(int(channel_id))
    if channel is not None:
        return channel
    try:
        return await bot.fetch_channel(int(channel_id))
    except Exception:
        return None


async def run_discord_cutover(bot: Any) -> CutoverReport:
    """Resolve legacy settings, make old cards inert, and migrate core state.

    The only configuration mutation is an in-memory promotion of one
    unambiguous legacy channel name to its resolved ID. Conflicting names are
    reported and external delivery remains disabled until the operator saves
    an explicit ``channel_id``.
    """
    config = bot.config.discord
    guild = getattr(bot, "_guild", None)
    conflicts: list[str] = []

    channel_id = str(config.channel_id or "")
    if not channel_id and config.legacy_destination_conflict:
        conflicts.append(
            "legacy destinations disagree: " + ", ".join(config.legacy_destination_names)
        )
    elif not channel_id and len(config.legacy_destination_names) == 1 and guild is not None:
        name = config.legacy_destination_names[0]
        matches = [channel for channel in guild.text_channels if channel.name == name]
        if len(matches) == 1:
            channel_id = str(matches[0].id)
            config.channel_id = channel_id
        elif not matches:
            conflicts.append(f"legacy destination #{name} was not found")
        else:
            conflicts.append(f"legacy destination #{name} is ambiguous")
    elif not channel_id:
        conflicts.append("discord.channel_id is not configured")

    inventory: dict[tuple[str, str], LegacyBinding] = {}
    inert_messages = 0
    bot_user_id = getattr(getattr(bot, "user", None), "id", None)
    questions = await bot.orchestrator.db.list_agent_questions(pending_only=True)
    for question in questions:
        legacy_channel = str(question.get("discord_channel_id") or "")
        message_id = str(question.get("discord_message_id") or "")
        channel = await _resolve_channel(bot, legacy_channel)
        if channel is None or not message_id.isdigit():
            continue
        try:
            message = await channel.fetch_message(int(message_id))
        except Exception:
            continue
        if getattr(getattr(message, "author", None), "id", None) != bot_user_id:
            continue
        try:
            await message.edit(view=None)
            inert_messages += 1
        except Exception:
            # Lack of edit permission is visible in the migration report. The
            # view still has no callback after restart, so it cannot mutate.
            conflicts.append(f"could not clear legacy question view {message_id}")
        inventory[("question", str(question["id"]))] = LegacyBinding(
            "question", str(question["id"]), legacy_channel, message_id
        )

    if guild is not None:
        names = set(config.legacy_inventory_names)
        channels = [channel for channel in guild.text_channels if channel.name in names]
        selected = await _resolve_channel(bot, channel_id)
        if selected is not None and selected not in channels:
            channels.append(selected)
        for channel in channels:
            try:
                history = channel.history(limit=500)
                async for message in history:
                    if getattr(getattr(message, "author", None), "id", None) != bot_user_id:
                        continue
                    gate_id = _gate_identity(message)
                    if gate_id is None:
                        continue
                    try:
                        await message.edit(view=None)
                        inert_messages += 1
                    except Exception:
                        conflicts.append(f"could not clear legacy gate view {message.id}")
                    inventory[("gate", gate_id)] = LegacyBinding(
                        "gate", gate_id, str(channel.id), str(message.id)
                    )
            except Exception:
                conflicts.append(f"could not inventory legacy channel {channel.id}")

    report = await LegacyConversationMigrator(bot.orchestrator.db).run(
        channel_id=channel_id,
        bindings=inventory.values(),
        inert_messages=inert_messages,
    )
    if conflicts:
        report.status = "needs_configuration"
        report.conflicts = tuple(dict.fromkeys(conflicts))
    return report

__all__ = [
    "CutoverReport",
    "LegacyBinding",
    "LegacyConversationMigrator",
    "run_discord_cutover",
]
