"""Frozen kind registry, built explicitly by the command/service composition root."""

import time
from collections.abc import Mapping
from types import MappingProxyType

from src.waits.adapters.base import invalid
from src.waits.conditions import ConditionAdapter


class ConditionRegistry:
    def __init__(self, adapters):
        entries = {}
        for adapter in adapters:
            if adapter.kind in entries:
                raise ValueError(f"duplicate condition kind: {adapter.kind}")
            entries[adapter.kind] = adapter
        self.adapters: Mapping[str, ConditionAdapter] = MappingProxyType(entries)

    def adapter(self, kind: str) -> ConditionAdapter:
        if not isinstance(kind, str) or kind not in self.adapters:
            invalid("a registered condition kind is required")
        return self.adapters[kind]

    def normalise(self, raw):
        if not isinstance(raw, Mapping):
            invalid()
        return self.adapter(raw.get("kind")).normalise(raw)

    async def verify(self, conn, cond, scope):
        return await self.adapter(cond.kind).verify(conn, cond, scope)

    async def observe(self, conn, cond):
        return await self.adapter(cond.kind).observe(conn, cond)


def build_registry(*, clock=time.time, ci_reader=None, pr_reader=None, resource_reader=None):
    """Missing service readers refuse verification; they never create an exemption."""
    from src.waits.adapters.batch import BatchAdapter
    from src.waits.adapters.ci import CIAdapter
    from src.waits.adapters.dependency import DependencyAdapter
    from src.waits.adapters.human import HumanAdapter
    from src.waits.adapters.job import JobAdapter
    from src.waits.adapters.message import MessageAdapter
    from src.waits.adapters.playbook_condition import PlaybookConditionAdapter
    from src.waits.adapters.pull_request import PullRequestAdapter
    from src.waits.adapters.resource import ResourceAdapter
    from src.waits.adapters.task import TaskAdapter
    from src.waits.adapters.timer import TimerAdapter

    return ConditionRegistry(
        [
            TaskAdapter(),
            DependencyAdapter(),
            JobAdapter(),
            CIAdapter("ci_check", ci_reader),
            CIAdapter("ci_run", ci_reader),
            PullRequestAdapter(pr_reader),
            BatchAdapter(),
            MessageAdapter(),
            HumanAdapter(clock),
            TimerAdapter(clock),
            ResourceAdapter(resource_reader),
            PlaybookConditionAdapter(),
        ]
    )
