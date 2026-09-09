"""Escalation delivery: one channel post and one thread per incident.

The core owns the incident (:mod:`src.database.queries.escalation_queries`);
this package owns nothing but presentation and transport.  It is split so the
interesting rules stay testable without a gateway:

* :mod:`src.escalations.facts` — the value objects, including the mention
  policy that is the only source of a ping this feature may emit;
* :mod:`src.escalations.render` — every message Discord ever sees, with
  authored text neutralised on the way in;
* :mod:`src.escalations.plan` — which deliveries an incident's current state
  implies, as a pure function of durable rows;
* :mod:`src.escalations.transport` — the narrow port, its honest fault
  taxonomy and the in-memory sink the tests use;
* :mod:`src.escalations.dispatch` — the pump that leases a delivery, sends it
  once, reconciles an ambiguous send and records the receipt.

The Discord implementation of the port lives in
:mod:`src.discord.escalation_transport`.
"""

from src.escalations.dispatch import EscalationDeliveryService, TickReport
from src.escalations.facts import (
    DELIVERY_KINDS,
    KIND_ACK,
    KIND_RELAY,
    KIND_RESOLUTION,
    KIND_ROOT,
    PRIORITY_DIGEST,
    PRIORITY_FOLLOWUP,
    PRIORITY_RESOLUTION,
    PRIORITY_ROOT,
    EscalationFacts,
    MentionPolicy,
    TransportBinding,
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
    render_relay,
    render_resolution,
    render_resolved_root,
    render_root,
    render_thread_opener,
    sanitise,
    thread_name,
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
    "DELIVERY_KINDS",
    "KIND_ACK",
    "KIND_RELAY",
    "KIND_RESOLUTION",
    "KIND_ROOT",
    "PRIORITY_DIGEST",
    "PRIORITY_FOLLOWUP",
    "PRIORITY_RESOLUTION",
    "PRIORITY_ROOT",
    "DeliveryPlan",
    "EscalationDeliveryService",
    "EscalationFacts",
    "EscalationTransport",
    "MentionPolicy",
    "PlannedDelivery",
    "SendOutcome",
    "SinkTransport",
    "TickReport",
    "TransportAmbiguous",
    "TransportBinding",
    "TransportError",
    "TransportMissing",
    "TransportRetryable",
    "TransportUnavailable",
    "binding_from_deliveries",
    "escalation_url",
    "marker_for",
    "plan_deliveries",
    "plan_replacement",
    "render_ack",
    "render_relay",
    "render_resolution",
    "render_resolved_root",
    "render_root",
    "render_thread_opener",
    "sanitise",
    "thread_name",
]
