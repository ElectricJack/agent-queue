"""Platform-agnostic messaging abstraction layer.

Provides the ``MessagingPort`` ABC that messaging transports implement, plus
the ``RichNotification`` / ``NotificationAction`` types used to describe
rich messages without coupling to any specific chat platform.  Today the
only transport is Discord; ``NullMessagingAdapter`` implements the same
contract as a no-op for ``messaging_platform: "none"``.

The ``MessagingAdapter`` ABC is the higher-level orchestrator-facing contract,
and ``create_messaging_adapter()`` is the factory that selects the correct
concrete adapter based on ``AppConfig.messaging_platform``.

Typical usage::

    from src.messaging import MessagingAdapter, create_messaging_adapter
    from src.messaging import MessagingPort, RichNotification, NotificationAction

See ``src/messaging/port.py`` for the low-level transport contract and
``src/messaging/base.py`` for the adapter ABC.
"""

from src.messaging.base import MessagingAdapter
from src.messaging.factory import create_messaging_adapter
from src.messaging.port import MessagingPort, RichNotification, NotificationAction
from src.messaging.types import (
    NotifyCallback,
    ThreadSendCallback,
    CreateThreadCallback,
    EditThreadRootCallback,
)

__all__ = [
    "MessagingAdapter",
    "create_messaging_adapter",
    "MessagingPort",
    "RichNotification",
    "NotificationAction",
    "NotifyCallback",
    "ThreadSendCallback",
    "CreateThreadCallback",
    "EditThreadRootCallback",
]
