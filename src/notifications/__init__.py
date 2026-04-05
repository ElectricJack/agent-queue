"""Event-driven notification layer for transport-agnostic notifications.

Notifications are emitted as typed events on the EventBus.  Consumers
(Discord, WebSocket, Slack, etc.) subscribe to ``notify.*`` events and
handle formatting/delivery independently.

See ``events.py`` for all event types and ``builder.py`` for helpers
that construct events from domain objects.
"""

from src.notifications.events import (
    AgentQuestionEvent,
    BudgetWarningEvent,
    ChainStuckEvent,
    MergeConflictEvent,
    NotifyEvent,
    PlanAwaitingApprovalEvent,
    PRCreatedEvent,
    PushFailedEvent,
    StuckDefinedTaskEvent,
    SystemOnlineEvent,
    TaskBlockedEvent,
    TaskCompletedEvent,
    TaskFailedEvent,
    TaskMessageEvent,
    TaskStartedEvent,
    TaskStoppedEvent,
    TaskThreadCloseEvent,
    TaskThreadOpenEvent,
    TextNotifyEvent,
)

__all__ = [
    "AgentQuestionEvent",
    "BudgetWarningEvent",
    "ChainStuckEvent",
    "MergeConflictEvent",
    "NotifyEvent",
    "PlanAwaitingApprovalEvent",
    "PRCreatedEvent",
    "PushFailedEvent",
    "StuckDefinedTaskEvent",
    "SystemOnlineEvent",
    "TaskBlockedEvent",
    "TaskCompletedEvent",
    "TaskFailedEvent",
    "TaskMessageEvent",
    "TaskStartedEvent",
    "TaskStoppedEvent",
    "TaskThreadCloseEvent",
    "TaskThreadOpenEvent",
    "TextNotifyEvent",
]
