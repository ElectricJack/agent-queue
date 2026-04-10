"""Integration layer for agent-queue.

Glue modules that connect core services to the orchestrator lifecycle,
event bus, and command handler.  Each integration registers callbacks
and wires up services to the task execution pipeline without polluting
the core modules with framework-specific concerns.
"""
