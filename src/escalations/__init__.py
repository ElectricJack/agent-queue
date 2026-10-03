"""Escalation delivery: one channel post and one thread per incident.

The core owns the incident (:mod:`src.database.queries.escalation_queries`);
this package owns nothing but presentation and transport.  It is split so the
interesting rules stay testable without a gateway:

* :mod:`src.escalations.facts` — the value objects, including the mention
  policy that is the only source of a ping this feature may emit;
* :mod:`src.escalations.render` — every message Discord ever sees, with
  authored text neutralised on the way in;
* :mod:`src.escalations.state` — the §5.2 state machine: which form the one
  post shows, as a pure function of the incident;
* :mod:`src.escalations.interactions` — §5.3's choice buttons: what a post
  offers, and what a press names (never what it says);
* :mod:`src.escalations.plan` — which deliveries an incident's current state
  implies, as a pure function of durable rows;
* :mod:`src.escalations.transport` — the narrow port, its honest fault
  taxonomy and the in-memory sink the tests use;
* :mod:`src.escalations.dispatch` — the pump that leases a delivery, sends it
  once, reconciles an ambiguous send and records the receipt;
* :mod:`src.escalations.autoresolve` — §5.5's rules, which close an incident
  whose source went away;
* :mod:`src.escalations.sweep` — §5.6's one-shot back-fill sweep over the pile
  those rules inherited, dry-run first;
* :mod:`src.escalations.intake` — the mirror of the planner: which inbound
  transport messages are allowed to become a verified human reply.

The Discord implementation of the port lives in
:mod:`src.discord.escalation_transport`.
"""

from src.escalations.autoresolve import (
    AUTO_OBSOLETE_TASK_STATUSES,
    AUTO_RESOLVE_RULES,
    STALE_OBSOLETE_SECONDS,
    AutoDecision,
    AutoResolveReport,
    EscalationAutoResolver,
    RuleContext,
)
from src.escalations.dispatch import EscalationDeliveryService, TickReport
from src.escalations.facts import (
    DELIVERY_KINDS,
    KIND_ACK,
    KIND_RELAY,
    KIND_RESOLUTION,
    KIND_ROOT,
    KIND_STATE,
    PRIORITY_DIGEST,
    PRIORITY_FOLLOWUP,
    PRIORITY_RESOLUTION,
    PRIORITY_ROOT,
    PRIORITY_STATE,
    EscalationFacts,
    MentionPolicy,
    TransportBinding,
)
from src.escalations.intake import (
    ACTION_ACCEPT,
    ACTION_CLOSED,
    ACTION_IGNORE,
    InboundMessage,
    IntakeDecision,
    classify_inbound,
)
from src.escalations.interactions import (
    KIND_CHOICE,
    KIND_REPLY,
    MAX_CHOICE_BUTTONS,
    REPLY_BUTTON_LABEL,
    ButtonPress,
    ButtonSpec,
    choice_buttons,
    choice_text,
    parse_custom_id,
)
from src.escalations.plan import (
    DeliveryPlan,
    PlannedDelivery,
    binding_from_deliveries,
    plan_deliveries,
    plan_replacement,
)
from src.escalations.render import (
    escalation_url,
    marker_for,
    render_ack,
    render_collapsed_root,
    render_relay,
    render_resolution,
    render_resolved_root,
    render_root,
    render_state_root,
    render_thread_opener,
    sanitise,
    thread_name,
)
from src.escalations.state import (
    COLLAPSED_STATES,
    DISPLAY_STATES,
    STATE_ANSWERED,
    STATE_OBSOLETE,
    STATE_OPEN,
    STATE_RESOLVED,
    STATE_STALE,
    display_state,
    incident_display_state,
    is_collapsed,
    is_stale_due,
    state_dedup_key,
    thread_archived,
)
from src.escalations.supervisor import SupervisorDeliveryWatchdog
from src.escalations.sweep import (
    ACTION_OBSOLETE,
    ACTION_RESOLVE,
    ACTION_TRIAGE,
    SWEEP_RULES,
    TARGET_OPEN_ITEMS,
    EscalationSweeper,
    SweepFacts,
    SweepItem,
    SweepPlan,
    SweepReport,
)
from src.escalations.transport import (
    EscalationTransport,
    SendOutcome,
    SinkTransport,
    TransportAmbiguous,
    TransportError,
    TransportMissing,
    TransportRetryable,
    TransportUnavailable,
)

__all__ = [
    "ACTION_ACCEPT",
    "ACTION_CLOSED",
    "ACTION_IGNORE",
    "ACTION_OBSOLETE",
    "ACTION_RESOLVE",
    "ACTION_TRIAGE",
    "AUTO_OBSOLETE_TASK_STATUSES",
    "AUTO_RESOLVE_RULES",
    "COLLAPSED_STATES",
    "DELIVERY_KINDS",
    "DISPLAY_STATES",
    "KIND_ACK",
    "KIND_CHOICE",
    "KIND_RELAY",
    "KIND_REPLY",
    "KIND_RESOLUTION",
    "KIND_ROOT",
    "KIND_STATE",
    "MAX_CHOICE_BUTTONS",
    "PRIORITY_DIGEST",
    "PRIORITY_FOLLOWUP",
    "PRIORITY_RESOLUTION",
    "PRIORITY_ROOT",
    "PRIORITY_STATE",
    "REPLY_BUTTON_LABEL",
    "STALE_OBSOLETE_SECONDS",
    "STATE_ANSWERED",
    "STATE_OBSOLETE",
    "STATE_OPEN",
    "STATE_RESOLVED",
    "STATE_STALE",
    "SWEEP_RULES",
    "TARGET_OPEN_ITEMS",
    "AutoDecision",
    "AutoResolveReport",
    "ButtonPress",
    "ButtonSpec",
    "DeliveryPlan",
    "EscalationAutoResolver",
    "EscalationDeliveryService",
    "EscalationFacts",
    "EscalationSweeper",
    "EscalationTransport",
    "InboundMessage",
    "IntakeDecision",
    "MentionPolicy",
    "PlannedDelivery",
    "RuleContext",
    "SendOutcome",
    "SinkTransport",
    "SupervisorDeliveryWatchdog",
    "SweepFacts",
    "SweepItem",
    "SweepPlan",
    "SweepReport",
    "TickReport",
    "TransportAmbiguous",
    "TransportBinding",
    "TransportError",
    "TransportMissing",
    "TransportRetryable",
    "TransportUnavailable",
    "binding_from_deliveries",
    "choice_buttons",
    "choice_text",
    "classify_inbound",
    "display_state",
    "escalation_url",
    "incident_display_state",
    "is_collapsed",
    "is_stale_due",
    "marker_for",
    "parse_custom_id",
    "plan_deliveries",
    "plan_replacement",
    "render_ack",
    "render_collapsed_root",
    "render_relay",
    "render_resolution",
    "render_resolved_root",
    "render_root",
    "render_state_root",
    "render_thread_opener",
    "sanitise",
    "state_dedup_key",
    "thread_archived",
    "thread_name",
]
