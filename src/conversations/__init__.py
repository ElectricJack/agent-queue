"""Discord @mention conversations with the global supervisor.

Opt-in (``discord.conversation.enabled``, off by default) and fail-closed.
Design: the mention-routing spec, vault
``projects/agent-queue/specs/2026-09-24-discord-mention-routing-to-the-supervisor.md``
(§4 is the authority boundary).  :mod:`.limits` holds every numeric bound,
:mod:`.preconditions` names what must hold before any message is accepted, and
:mod:`.outbox` is the port every outbound side effect is enqueued through.
"""
