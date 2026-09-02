from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.agent_metrics import AgentMetrics
    from ..models.daemon_metrics import DaemonMetrics
    from ..models.machine_metrics import MachineMetrics
    from ..models.sampler_metrics import SamplerMetrics
    from ..models.slot_metrics import SlotMetrics
    from ..models.stall_metrics import StallMetrics
    from ..models.subagent_metrics import SubagentMetrics
    from ..models.task_metrics import TaskMetrics
    from ..models.throughput_metrics import ThroughputMetrics
    from ..models.token_metrics import TokenMetrics


T = TypeVar("T", bound="MetricsSample")


@_attrs_define
class MetricsSample:
    """One point on every series.

    ``ts`` is the bucket start, not the collection instant, so points line up
    exactly across resolutions.

        Attributes:
            ts (float):
            agents (AgentMetrics | Unset): Live sessions, split the three ways the tab graphs them.
            tasks (TaskMetrics | Unset):
            subagents (SubagentMetrics | Unset): Fleet sub-agent totals plus the per-session drill-down.

                ``complete`` is the conjunction over live sessions: one session without
                hooks makes ``native`` and ``total`` lower bounds for the whole fleet.
            tokens (TokenMetrics | Unset): Rates over the trailing 60 seconds of the token ledger.

                ``unattributed_per_min`` is ledger volume that carried no input/output
                split — reported separately rather than folded into a model's rate, the
                same honesty rule ``get_costs`` applies to pricing.
            slots (SlotMetrics | Unset): Worktree slots.  ``cap`` is null when worktree execution is off.
            machine (MachineMetrics | Unset): Nulls mean the platform does not expose the value, not zero.
            daemon (DaemonMetrics | Unset):
            stall (StallMetrics | Unset): Stall-ladder activity in the trailing hour.

                Sourced from bus events the reconciler does not persist, so both counters
                restart with the daemon — read them next to ``daemon.uptime_seconds``.
            throughput (ThroughputMetrics | Unset):
            merges_per_hour (float | Unset):  Default: 0.0.
            sampler (SamplerMetrics | Unset): The sampler's own per-tick cost, so its overhead is observable.
    """

    ts: float
    agents: AgentMetrics | Unset = UNSET
    tasks: TaskMetrics | Unset = UNSET
    subagents: SubagentMetrics | Unset = UNSET
    tokens: TokenMetrics | Unset = UNSET
    slots: SlotMetrics | Unset = UNSET
    machine: MachineMetrics | Unset = UNSET
    daemon: DaemonMetrics | Unset = UNSET
    stall: StallMetrics | Unset = UNSET
    throughput: ThroughputMetrics | Unset = UNSET
    merges_per_hour: float | Unset = 0.0
    sampler: SamplerMetrics | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        ts = self.ts

        agents: dict[str, Any] | Unset = UNSET
        if not isinstance(self.agents, Unset):
            agents = self.agents.to_dict()

        tasks: dict[str, Any] | Unset = UNSET
        if not isinstance(self.tasks, Unset):
            tasks = self.tasks.to_dict()

        subagents: dict[str, Any] | Unset = UNSET
        if not isinstance(self.subagents, Unset):
            subagents = self.subagents.to_dict()

        tokens: dict[str, Any] | Unset = UNSET
        if not isinstance(self.tokens, Unset):
            tokens = self.tokens.to_dict()

        slots: dict[str, Any] | Unset = UNSET
        if not isinstance(self.slots, Unset):
            slots = self.slots.to_dict()

        machine: dict[str, Any] | Unset = UNSET
        if not isinstance(self.machine, Unset):
            machine = self.machine.to_dict()

        daemon: dict[str, Any] | Unset = UNSET
        if not isinstance(self.daemon, Unset):
            daemon = self.daemon.to_dict()

        stall: dict[str, Any] | Unset = UNSET
        if not isinstance(self.stall, Unset):
            stall = self.stall.to_dict()

        throughput: dict[str, Any] | Unset = UNSET
        if not isinstance(self.throughput, Unset):
            throughput = self.throughput.to_dict()

        merges_per_hour = self.merges_per_hour

        sampler: dict[str, Any] | Unset = UNSET
        if not isinstance(self.sampler, Unset):
            sampler = self.sampler.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "ts": ts,
            }
        )
        if agents is not UNSET:
            field_dict["agents"] = agents
        if tasks is not UNSET:
            field_dict["tasks"] = tasks
        if subagents is not UNSET:
            field_dict["subagents"] = subagents
        if tokens is not UNSET:
            field_dict["tokens"] = tokens
        if slots is not UNSET:
            field_dict["slots"] = slots
        if machine is not UNSET:
            field_dict["machine"] = machine
        if daemon is not UNSET:
            field_dict["daemon"] = daemon
        if stall is not UNSET:
            field_dict["stall"] = stall
        if throughput is not UNSET:
            field_dict["throughput"] = throughput
        if merges_per_hour is not UNSET:
            field_dict["merges_per_hour"] = merges_per_hour
        if sampler is not UNSET:
            field_dict["sampler"] = sampler

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_metrics import AgentMetrics
        from ..models.daemon_metrics import DaemonMetrics
        from ..models.machine_metrics import MachineMetrics
        from ..models.sampler_metrics import SamplerMetrics
        from ..models.slot_metrics import SlotMetrics
        from ..models.stall_metrics import StallMetrics
        from ..models.subagent_metrics import SubagentMetrics
        from ..models.task_metrics import TaskMetrics
        from ..models.throughput_metrics import ThroughputMetrics
        from ..models.token_metrics import TokenMetrics

        d = dict(src_dict)
        ts = d.pop("ts")

        _agents = d.pop("agents", UNSET)
        agents: AgentMetrics | Unset
        if isinstance(_agents, Unset):
            agents = UNSET
        else:
            agents = AgentMetrics.from_dict(_agents)

        _tasks = d.pop("tasks", UNSET)
        tasks: TaskMetrics | Unset
        if isinstance(_tasks, Unset):
            tasks = UNSET
        else:
            tasks = TaskMetrics.from_dict(_tasks)

        _subagents = d.pop("subagents", UNSET)
        subagents: SubagentMetrics | Unset
        if isinstance(_subagents, Unset):
            subagents = UNSET
        else:
            subagents = SubagentMetrics.from_dict(_subagents)

        _tokens = d.pop("tokens", UNSET)
        tokens: TokenMetrics | Unset
        if isinstance(_tokens, Unset):
            tokens = UNSET
        else:
            tokens = TokenMetrics.from_dict(_tokens)

        _slots = d.pop("slots", UNSET)
        slots: SlotMetrics | Unset
        if isinstance(_slots, Unset):
            slots = UNSET
        else:
            slots = SlotMetrics.from_dict(_slots)

        _machine = d.pop("machine", UNSET)
        machine: MachineMetrics | Unset
        if isinstance(_machine, Unset):
            machine = UNSET
        else:
            machine = MachineMetrics.from_dict(_machine)

        _daemon = d.pop("daemon", UNSET)
        daemon: DaemonMetrics | Unset
        if isinstance(_daemon, Unset):
            daemon = UNSET
        else:
            daemon = DaemonMetrics.from_dict(_daemon)

        _stall = d.pop("stall", UNSET)
        stall: StallMetrics | Unset
        if isinstance(_stall, Unset):
            stall = UNSET
        else:
            stall = StallMetrics.from_dict(_stall)

        _throughput = d.pop("throughput", UNSET)
        throughput: ThroughputMetrics | Unset
        if isinstance(_throughput, Unset):
            throughput = UNSET
        else:
            throughput = ThroughputMetrics.from_dict(_throughput)

        merges_per_hour = d.pop("merges_per_hour", UNSET)

        _sampler = d.pop("sampler", UNSET)
        sampler: SamplerMetrics | Unset
        if isinstance(_sampler, Unset):
            sampler = UNSET
        else:
            sampler = SamplerMetrics.from_dict(_sampler)

        metrics_sample = cls(
            ts=ts,
            agents=agents,
            tasks=tasks,
            subagents=subagents,
            tokens=tokens,
            slots=slots,
            machine=machine,
            daemon=daemon,
            stall=stall,
            throughput=throughput,
            merges_per_hour=merges_per_hour,
            sampler=sampler,
        )

        metrics_sample.additional_properties = d
        return metrics_sample

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
