"""Executable V1/V2 shadow-parity harness — Package 6 T-10, T-11, T-12.

Both arms are *run*, not described.  The V1 arm is ``tests/conftest.py``'s
``PipelineEngine`` — the same rule selection, ``event.task`` hydration and
``_eval_pipeline_when`` guard evaluation the shipped orchestrator uses — driven
from the frozen pre-rewrite graph at
``tests/fixtures/playbooks/v1/default-pipeline.md``.  The V2 arm is the real
``PlaybookEngine.dispatch_event(..., ExecutionMode.SHADOW)`` over the reviewed
artifact in ``tests/fixtures/playbooks/v2/default-pipeline/``.

Reconciliation against the live tree (child plan §3.8: record deviations, never
substitute silently):

* **Shadow stops at the binding frontier.**  ``ShadowCommandExecutor`` records a
  step's arguments and returns ``UNRESOLVED`` without a value, so any later step
  reading a ``save_result_as`` binding fails input resolution and the symbolic
  path ends.  Shadow alone therefore observes only each rule's first,
  event-derived command — it cannot produce the loop iterations §5.4's corpus
  is written to exercise.  Past that frontier the V2 arm is *projected* from the
  artifact: :func:`_project_rule` walks the artifact's own typed steps with the
  engine's own :func:`resolve_value` / :class:`ResolutionScope`, taking each
  boundary's outcome from the same scripted oracle the V1 arm's recording
  handler answers with.  The projection is pinned to the engine by
  ``test_projection_agrees_with_the_engine_at_the_shadow_frontier``: wherever
  shadow *does* resolve, the two must record identical arguments.
* **No contract marks arguments ``free_text``**, so §3.5.1's rule 3 (whitespace
  normalisation inside free-text values) has nothing to apply to and is a no-op
  here.  Rules 1 (sensitive-argument redaction, via
  :func:`src.commands.contracts.models.redact_args`) and 2 (generated ids to
  ``<gen:N>``) are applied.
* **``AuthzDecision`` is Package 6's own record**, not
  ``src.commands.authorization.AuthzDecision``; the V2 arm derives it from
  :func:`authorize_command` under the reviewed ``capabilities_granted`` policy,
  and the V1 arm records ``allowed=True`` because V1 performs no capability
  check at all.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.commands.authorization import MODE_ENFORCE, authorize_command
from src.commands.contracts.builtin import register_builtin_contracts
from src.commands.contracts.models import redact_args
from src.commands.contracts.registry import ContractRegistry
from src.commands.principal import ExecutionPrincipal
from src.playbooks.definition import (
    CommandStep,
    ForEachStep,
    PlaybookDefinition,
    Rule,
    TerminalStep,
    load_definition_json,
)
from src.playbooks.engine import PlaybookEngine
from src.playbooks.executors.base import EngineServices, ExecutionMode
from src.playbooks.expressions import (
    ResolutionScope,
    ValueResolutionError,
    resolve_value,
)
from src.playbooks.migration import AuthzDecision, CommandInvocation, ShadowObservation
from src.profiles.capabilities import CapabilityPolicy

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "v2" / "events"
V2_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "v2" / "default-pipeline"
V1_SOURCE = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "v1" / "default-pipeline.md"
PARITY_REPORT = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "v2" / "parity-report.json"

#: The four shipped playbooks, and whether their V1 predecessor carried a
#: machine graph.  Only a deterministic one can be compared per run; the other
#: three are compared structurally (§4.5) and recorded as a coverage limit.
DETERMINISTIC_PLAYBOOKS: tuple[str, ...] = ("default-pipeline",)
STRUCTURAL_ONLY_PLAYBOOKS: tuple[str, ...] = (
    "default-assignment-routing",
    "memory-consolidation",
    "coding-reflection",
)


# ---------------------------------------------------------------------------
# The scripted oracle — one answer per command, shared by both arms
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScriptedResult:
    """One command's answer, expressed once for both arms.

    ``outcome`` is the V2 contract outcome; ``value`` is the declared result
    model's fields.  The V1 arm's recording handler returns
    ``{"success": <outcome is a success>, **value}``, which is the shape
    ``pipeline_runner`` reads.
    """

    outcome: str
    value: Mapping[str, Any] = field(default_factory=dict)
    success: bool = True
    error: str | None = None
    #: When set, the V1 handler double raises instead of returning.  Only the
    #: ``rule-failure-isolation`` demonstration uses it: a raising command is
    #: the one way this pipeline can end a rule as ``failed``, because every
    #: authored ``on_failure`` edge routes to the rule's ``done`` terminal.
    raises: str | None = None

    def v1_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"success": self.success, **self.value}
        if self.error is not None:
            payload["error"] = self.error
        return payload


#: Ids the oracle mints during an observation.  §3.5.1 rule 2 replaces them
#: with ``<gen:N>`` in first-appearance order, per arm.
GENERATED_IDS: frozenset[str] = frozenset(
    {"parity-review-task", "parity-final-task", "parity-spec-task", "parity-gate"}
)

DEFAULT_ORACLE: Mapping[str, ScriptedResult] = {
    "ensure_task": ScriptedResult("created", {"task_id": "parity-review-task", "created": True}),
    "add_dependency": ScriptedResult("linked", {"linked": True}),
    "get_downstream_tasks": ScriptedResult("listed", {"tasks": [], "count": 0}),
    "gate_create": ScriptedResult("created", {"gate_id": "parity-gate", "created": True}),
    "task_batch_commit": ScriptedResult(
        "committed", {"committed": True, "task_ids": ["parity-spec-task"]}
    ),
}

_DOWNSTREAM_TWO = ScriptedResult(
    "listed",
    {"tasks": [{"id": "solid-harbor.20"}, {"id": "solid-harbor.21"}], "count": 2},
)
_ADD_DEPENDENCY_FAILS = ScriptedResult(
    "rejected", {}, success=False, error="dependency target is closed"
)

#: Per-event oracle overrides.  Everything else answers from
#: :data:`DEFAULT_ORACLE`.
ORACLE_OVERRIDES: Mapping[str, Mapping[str, ScriptedResult]] = {
    "task-completed-with-downstream": {"get_downstream_tasks": _DOWNSTREAM_TWO},
    "task-completed-with-branch-and-pr": {"get_downstream_tasks": _DOWNSTREAM_TWO},
    "task-completed-add-dependency-fails": {"add_dependency": _ADD_DEPENDENCY_FAILS},
}


@dataclass(frozen=True, slots=True)
class CorpusCase:
    name: str
    event: Mapping[str, Any]
    oracle: Mapping[str, ScriptedResult]

    @property
    def event_id(self) -> str:
        return str(self.event["event_id"])

    @property
    def event_type(self) -> str:
        return str(self.event.get("_event_type") or self.event.get("type"))

    def answer(self, command: str) -> ScriptedResult:
        if command in self.oracle:
            return self.oracle[command]
        return DEFAULT_ORACLE[command]


def load_corpus() -> tuple[CorpusCase, ...]:
    """Every ``tests/fixtures/playbooks/v2/events/*.json``, name-ordered."""
    cases = []
    for path in sorted(CORPUS_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        cases.append(
            CorpusCase(
                name=path.stem,
                event=payload,
                oracle=ORACLE_OVERRIDES.get(path.stem, {}),
            )
        )
    return tuple(cases)


# ---------------------------------------------------------------------------
# §3.5.1 canonicalisation
# ---------------------------------------------------------------------------


class _Canonicaliser:
    """Per-arm argument canonicalisation.  One instance per observation."""

    def __init__(self, registry: ContractRegistry) -> None:
        self._registry = registry
        self._placeholders: dict[str, str] = {}

    def _placeholder(self, value: str) -> str:
        if value not in self._placeholders:
            self._placeholders[value] = f"<gen:{len(self._placeholders)}>"
        return self._placeholders[value]

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            if value in GENERATED_IDS:
                return self._placeholder(value)
            return value
        if isinstance(value, Mapping):
            return {key: self._scrub(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._scrub(item) for item in value]
        return value

    def args(self, command: str, args: Mapping[str, Any]) -> str:
        registration = self._registry.get(command)
        redacted = (
            redact_args(registration.contract, dict(args)) if registration else dict(args)
        )
        return json.dumps(
            self._scrub(redacted), sort_keys=True, separators=(",", ":"), default=str
        )

    def outputs(self, bindings: Mapping[str, Any]) -> dict[str, Any]:
        return {name: self._scrub(value) for name, value in sorted(bindings.items())}


def contract_registry() -> ContractRegistry:
    registry = ContractRegistry()
    register_builtin_contracts(registry)
    return registry


# ---------------------------------------------------------------------------
# Step identity, shared by both arms
# ---------------------------------------------------------------------------


def v1_node_id(step_id: str, artifact: PlaybookDefinition) -> str:
    """Map a V2 step id onto the V1 node id it was lowered from.

    V2 namespaces step ids as ``<rule-id>--<v1-node-id>`` and appends
    ``-body`` to a loop body.  The body is folded into its ``foreach`` parent:
    the loop frame's shape is a registered expected difference
    (``loop-frame-shape``) and comparing it would assert the shape rather than
    the decisions.
    """
    step = artifact.steps[step_id]
    return f"{step.rule}-{step.title}"


# ---------------------------------------------------------------------------
# V1 arm
# ---------------------------------------------------------------------------


class RecordingHandler:
    """``CommandHandler.execute`` as a recorder.  It reaches nothing real."""

    def __init__(self, case: CorpusCase) -> None:
        self._case = case
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, command: str, args: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append((command, dict(args)))
        answer = self._case.answer(command)
        if answer.raises is not None:
            raise RuntimeError(answer.raises)
        return answer.v1_payload()


class _PathRecorder:
    """The only method ``PipelineRunner`` calls on its ``db``."""

    def __init__(self) -> None:
        self.nodes: list[str] = []

    async def update_playbook_run(self, _run_id: str, **fields: Any) -> None:
        node = fields.get("current_node")
        if node is not None:
            self.nodes.append(str(node))


def _compiled_v1_graph():
    from src.playbooks.pipeline_compiler import compile_pipeline

    result = compile_pipeline(V1_SOURCE.read_text(encoding="utf-8"))
    assert result.success, f"frozen V1 pipeline no longer compiles: {result.errors}"
    return result.playbook


def _binding_commands() -> dict[str, str]:
    """``output.as`` -> the V1 command that produced it, read off the graph."""
    graph = _compiled_v1_graph().to_dict()
    mapping: dict[str, str] = {}
    for node in graph["nodes"].values():
        action = node.get("action") if isinstance(node.get("action"), Mapping) else node
        output = (action or {}).get("output") or {}
        if output.get("as"):
            mapping[output["as"]] = str(action.get("command"))
    return mapping


async def run_v1_arm(case: CorpusCase, artifact: PlaybookDefinition) -> ShadowObservation:
    """Dispatch the frozen V1 graph through the shipped V1 dispatch helper."""
    from tests.conftest import PipelineEngine

    handler = RecordingHandler(case)
    recorder = _PathRecorder()
    engine = PipelineEngine(
        _compiled_v1_graph(),
        handler,
        db=None,
        runner_db=recorder,
        abort_on_rule_failure=True,
    )
    records = await engine.dispatch(
        case.event_type, dict(case.event), event_id=case.event_id
    )

    entry_to_rule = {
        v1_node_id(rule.entry_step, artifact): rule.id for rule in artifact.rules
    }
    rules_selected = tuple(
        entry_to_rule[entry] for entry, _result in records if entry in entry_to_rule
    )
    terminal = "completed"
    outputs: dict[str, Any] = {}
    for _entry, result in records:
        outputs.update(result.outputs or {})
        if result.status != "completed":
            terminal = result.status

    # V1 binds the handler's raw dict (``{"success": true, ...}``) while V2
    # binds the *declared result model* (``executors/command.py`` step 5).  The
    # comparable part is the declared fields, so both arms are projected onto
    # them; the ``success`` flag V1 adds is the shape of V1's handler protocol,
    # not a decision.
    outputs = {
        name: {
            key: item
            for key, item in value.items()
            if key in case.answer(_binding_commands()[name]).value
        }
        if isinstance(value, Mapping) and name in _binding_commands()
        else value
        for name, value in outputs.items()
    }

    canonical = _Canonicaliser(contract_registry())
    commands = tuple(
        CommandInvocation(order, name, canonical.args(name, args))
        for order, (name, args) in enumerate(handler.calls)
    )
    return ShadowObservation(
        arm="v1",
        event_id=case.event_id,
        event_type=case.event_type,
        rules_selected=rules_selected,
        node_path=tuple(recorder.nodes),
        commands=commands,
        routing_outputs=canonical.outputs(outputs),
        terminal=terminal,
        authorization=tuple(
            # V1 dispatches with no capability check whatsoever; recording the
            # absence as an allow is what makes a V2 denial visible as a
            # difference rather than as a missing field.
            AuthzDecision(name, "service", True, None)
            for name, _args in handler.calls
        ),
    )


# ---------------------------------------------------------------------------
# V2 arm
# ---------------------------------------------------------------------------


class _BuiltinOnlyResolver:
    """Every contracted command in this artifact is a built-in."""

    def is_builtin(self, _name: str) -> bool:
        return True

    def is_plugin(self, _name: str) -> bool:
        return False

    def plugin_command_names(self) -> frozenset[str]:
        return frozenset()


class _RaisingDependency:
    """Any attribute access is a shadow-arm side effect (§4.4)."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the shadow arm must not touch {name}")


class RaisingHandler:
    """The negative assertion of §4.4, as a handler double."""

    async def execute(self, command: str, _args: Mapping[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"no arm may execute {command}")


def load_v2_artifact() -> PlaybookDefinition:
    return load_definition_json((V2_FIXTURE / "artifact.json").read_text(encoding="utf-8"))


def reviewed_policy() -> CapabilityPolicy:
    """The capability set the checked-in ``review.md`` approved."""
    import re

    text = (V2_FIXTURE / "review.md").read_text(encoding="utf-8")
    match = re.search(r"^\s*aq_commands:\s*\[(.*?)\]\s*$", text, re.MULTILINE)
    assert match, "review.md no longer declares capabilities_granted.aq_commands"
    names = [name.strip() for name in match.group(1).split(",") if name.strip()]
    return CapabilityPolicy.from_namespaces(aq_commands=names)


def shadow_engine(artifact: PlaybookDefinition, *, handler: Any | None = None) -> PlaybookEngine:
    from tests.playbook_v2_engine_helpers import (
        InMemoryArtifactStore,
        RecordingRunRepository,
        artifact_ref_for,
    )

    store = InMemoryArtifactStore()
    store.put(artifact)
    ref = artifact_ref_for(artifact)

    class _Activations:
        async def ready_activations(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            return [ref]

    class _RaisingBus:
        async def emit(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("the shadow arm must not emit bus events")

    services = EngineServices(
        contracts=contract_registry(),
        clock=lambda: 1_000.0,
        artifact_store=store,
        bus=_RaisingBus(),
        handler=handler if handler is not None else _RaisingDependency(),
        llm=_RaisingDependency(),
        resolver=_BuiltinOnlyResolver(),
        authorization_mode=MODE_ENFORCE,
    )
    return PlaybookEngine(
        services=services, runs=RecordingRunRepository(), activations=_Activations()
    )


def parity_principal() -> ExecutionPrincipal:
    from src.commands.principal import PrincipalKind

    return ExecutionPrincipal(
        kind=PrincipalKind.SERVICE,
        policy=reviewed_policy(),
        service_name="playbook-shadow-parity",
    )


@dataclass
class _Projection:
    node_path: list[str] = field(default_factory=list)
    #: ``(step_id, command, resolved_args)`` in visit order.
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    bindings: dict[str, Any] = field(default_factory=dict)
    terminal: str = "completed"


_MAX_PROJECTION_STEPS = 2_000


def _project_rule(
    artifact: PlaybookDefinition,
    rule: Rule,
    event: Mapping[str, Any],
    case: CorpusCase,
    out: _Projection,
) -> None:
    """Walk one rule of the artifact, taking each outcome from the oracle.

    Everything the walk *decides* comes from the artifact — the step's typed
    inputs, its ``transitions`` map, the loop's ``collection`` and
    ``item_binding`` — and every value is resolved with the engine's own
    :func:`resolve_value`.  The only thing supplied from outside is the
    boundary outcome, which shadow cannot know and which the V1 arm's
    recording handler answers with the same value.
    """
    scope = ResolutionScope(event=event)
    step_id: str | None = rule.entry_step
    frames: list[tuple[str, list[Any], int]] = []
    visits = 0

    while step_id is not None:
        visits += 1
        assert visits < _MAX_PROJECTION_STEPS, f"projection did not terminate in {rule.id}"
        step = artifact.steps[step_id]
        node = v1_node_id(step_id, artifact)
        if not out.node_path or out.node_path[-1] != node:
            out.node_path.append(node)

        if isinstance(step, TerminalStep):
            if step.outcome != "completed":
                out.terminal = step.outcome
            return

        if isinstance(step, ForEachStep):
            if frames and frames[-1][0] == step_id:
                name, items, index = frames.pop()
                index += 1
            else:
                try:
                    items = resolve_value(step.collection, scope)
                except ValueResolutionError:
                    out.terminal = "failed"
                    return
                if not isinstance(items, list):
                    out.terminal = "failed"
                    return
                name, index = step_id, 0
            if index < len(items):
                frames.append((name, items, index))
                scope = ResolutionScope(
                    event=scope.event, context=scope.context, bindings=scope.bindings
                ).with_loop_item(step.item_binding, items[index], index)
                step_id = step.body_entry
                continue
            scope = ResolutionScope(
                event=scope.event, context=scope.context, bindings=scope.bindings
            )
            step_id = step.continuation or step.transitions.get("completed")
            continue

        assert isinstance(step, CommandStep), f"unexpected step kind {type(step).__name__}"
        try:
            args = {
                name: resolve_value(value, scope) for name, value in step.inputs.items()
            }
        except ValueResolutionError:
            out.terminal = "failed"
            return
        out.calls.append((step_id, step.command, args))
        answer = case.answer(step.command)
        if step.save_result_as and answer.value:
            out.bindings[step.save_result_as] = dict(answer.value)
            scope = scope.model_copy(
                update={"bindings": {**scope.bindings, step.save_result_as: dict(answer.value)}}
            )
        step_id = step.transitions.get(answer.outcome) or step.transitions.get("runtime_error")


async def run_v2_arm(
    case: CorpusCase, artifact: PlaybookDefinition
) -> tuple[ShadowObservation, Any, _Projection]:
    """Dispatch the reviewed artifact in shadow, then project past its frontier.

    Returns the observation, the engine's own ``DispatchResult`` and the
    projection, so a test can assert the two agree wherever shadow resolved
    anything.
    """
    engine = shadow_engine(artifact)
    principal = parity_principal()
    dispatch = await engine.dispatch_event(
        dict(case.event), principal, mode=ExecutionMode.SHADOW
    )

    out = _Projection()
    by_id = {rule.id: rule for rule in artifact.rules}
    for rule_id in dispatch.rules_selected:
        _project_rule(artifact, by_id[rule_id], dict(case.event), case, out)
        if out.terminal != "completed":
            # V1 shares one run row per event and stops at the first rule that
            # does not complete (``src/orchestrator/core.py``).  Stopping here
            # too keeps ``rule-failure-isolation`` a *registered* difference
            # rather than an artefact of the harness running further.
            break

    canonical = _Canonicaliser(contract_registry())
    resolver = _BuiltinOnlyResolver()
    commands = tuple(
        CommandInvocation(order, name, canonical.args(name, args))
        for order, (_step_id, name, args) in enumerate(out.calls)
    )
    authorization = []
    for _step_id, name, _args in out.calls:
        decision = authorize_command(name, principal, resolver=resolver, mode=MODE_ENFORCE)
        authorization.append(
            AuthzDecision(
                name,
                principal.kind.value,
                decision.allowed,
                None if decision.allowed else (decision.reason or "denied"),
            )
        )

    observation = ShadowObservation(
        arm="v2",
        event_id=case.event_id,
        event_type=case.event_type,
        rules_selected=tuple(dispatch.rules_selected),
        node_path=tuple(out.node_path),
        commands=commands,
        routing_outputs=canonical.outputs(out.bindings),
        terminal=out.terminal,
        authorization=tuple(authorization),
    )
    return observation, dispatch, out


# ---------------------------------------------------------------------------
# §4.5 — structural parity for the playbooks with no V1 machine graph
# ---------------------------------------------------------------------------

#: The reviewed V2 source fixture, and the shipped V1 source it was taken from.
STRUCTURAL_SOURCES: Mapping[str, str] = {
    "default-assignment-routing": "src/prompts/default_playbooks/default-assignment-routing.md",
    "memory-consolidation": "src/prompts/default_playbooks/memory-consolidation.md",
    "coding-reflection": "src/prompts/default_agent_type_playbooks/claude-opus/reflection.md",
}

#: Source fields retained across the reviewed copy.  This is only the source
#: half of structural parity; :func:`structural_parity` also projects the V2
#: artifact itself below.
STRUCTURAL_SOURCE_FIELDS: tuple[str, ...] = (
    "id",
    "kind",
    "role",
    "profile_id",
    "scope",
    "triggers",
    "cooldown",
    "max_tokens",
    "llm_config",
    "transition_llm_config",
    "output_schema",
)


#: The reviewed interpretation of each prose-only source's rule topology.
#: Triggers and profiles come from the shipped source frontmatter.  The
#: remaining values are the explicit capabilities, budgets, schemas and edges
#: recorded in each review's semantic decision.  They deliberately live here,
#: rather than being read back from either ``review.md`` or ``artifact.json``:
#: review capabilities are only an upper bound (§4.1), while these exact
#: expectations must be able to detect artifact drift.
STRUCTURAL_RULES: Mapping[str, tuple[tuple[str, str, Mapping[str, str], Mapping[str, str]], ...]] = {
    "default-assignment-routing": (
        (
            "assignment-route",
            "assignment-route--choose",
            {
                "completed": "assignment-route--done",
                "runtime_error": "assignment-route--done",
            },
            {"assignment-route--done": "completed"},
        ),
    ),
    "memory-consolidation": (
        (
            "memory-consolidation",
            "memory-consolidation--run",
            {
                "completed": "memory-consolidation--done",
                "runtime_error": "memory-consolidation--failed",
            },
            {
                "memory-consolidation--done": "completed",
                "memory-consolidation--failed": "failed",
            },
        ),
    ),
    "coding-reflection": (
        (
            "reflect-completed",
            "reflect-completed--run",
            {
                "completed": "reflect-completed--done",
                "runtime_error": "reflect-completed--failed",
            },
            {
                "reflect-completed--done": "completed",
                "reflect-completed--failed": "failed",
            },
        ),
        (
            "reflect-failed",
            "reflect-failed--run",
            {
                "completed": "reflect-failed--done",
                "runtime_error": "reflect-failed--failed",
            },
            {
                "reflect-failed--done": "completed",
                "reflect-failed--failed": "failed",
            },
        ),
    ),
}

STRUCTURAL_PURPOSES: Mapping[str, str] = {
    "default-assignment-routing": "assignment_routing",
    "memory-consolidation": "routine",
    "coding-reflection": "routine",
}

STRUCTURAL_BUDGETS: Mapping[str, Mapping[str, int]] = {
    "default-assignment-routing": {
        "max_calls": 1,
        "max_output_tokens": 4096,
        "max_total_tokens": 4096,
        "timeout_seconds": 300,
    },
    "memory-consolidation": {
        "max_calls": 50,
        "max_output_tokens": 4096,
        "max_total_tokens": 65536,
        "timeout_seconds": 900,
    },
    "coding-reflection": {
        "max_calls": 20,
        "max_output_tokens": 4096,
        "max_total_tokens": 32768,
        "timeout_seconds": 600,
    },
}

STRUCTURAL_CAPABILITIES: Mapping[str, Mapping[str, Any]] = {
    "default-assignment-routing": {
        "enabled": False,
        "aq_commands": [],
        "harness_tools": [],
        "plugin_tools": [],
    },
    "memory-consolidation": {
        "enabled": True,
        "aq_commands": ["create_task", "list_projects", "render_prompt"],
        "harness_tools": [],
        "plugin_tools": ["count_project_memory_files", "read_project_memory_file"],
    },
    "coding-reflection": {
        "enabled": True,
        "aq_commands": ["get_task"],
        "harness_tools": [],
        "plugin_tools": ["git_diff", "memory_save", "memory_search"],
    },
}

STRUCTURAL_OUTPUT_SCHEMAS: Mapping[str, Mapping[str, Any]] = {
    "default-assignment-routing": {"additionalProperties": True, "type": "object"},
    "memory-consolidation": {
        "additionalProperties": False,
        "properties": {
            "tasks_created": {
                "items": {
                    "additionalProperties": False,
                    "properties": {
                        "project_id": {"type": "string"},
                        "task_id": {"type": "string"},
                    },
                    "required": ["project_id", "task_id"],
                    "type": "object",
                },
                "type": "array",
            }
        },
        "required": ["tasks_created"],
        "type": "object",
    },
    "coding-reflection": {
        "additionalProperties": False,
        "properties": {
            "insights_saved": {"minimum": 0, "type": "integer"},
            "skipped": {"type": "boolean"},
            "summary": {"type": "string"},
        },
        "required": ["insights_saved", "skipped", "summary"],
        "type": "object",
    },
}


def _frontmatter(text: str) -> dict[str, Any]:
    import yaml

    if not text.startswith("---"):
        return {}
    _, _, rest = text.partition("---\n")
    body, _, _ = rest.partition("\n---")
    return yaml.safe_load(body) or {}


def _scope_projection(scope: object) -> dict[str, str]:
    if scope == "system":
        return {"type": "system"}
    prefix = "agent-type:"
    if isinstance(scope, str) and scope.startswith(prefix):
        return {"type": "agent_type", "agent_type": scope.removeprefix(prefix)}
    raise ValueError(f"unsupported structural-parity scope: {scope!r}")


def _capability_projection(capabilities: Mapping[str, Any]) -> dict[str, Any]:
    aq_commands = sorted(capabilities.get("aq_commands", ()))
    harness_tools = sorted(capabilities.get("harness_tools", ()))
    plugin_tools = sorted(capabilities.get("plugin_tools", ()))
    return {
        "enabled": bool(aq_commands or harness_tools or plugin_tools),
        "aq_commands": aq_commands,
        "harness_tools": harness_tools,
        "plugin_tools": plugin_tools,
    }


def _expected_artifact_projection(
    playbook_id: str,
    source: Mapping[str, Any],
) -> dict[str, Any]:
    topology = STRUCTURAL_RULES[playbook_id]
    triggers = tuple(source.get("triggers", ()))
    if len(topology) != len(triggers):
        raise ValueError(f"{playbook_id}: reviewed rule count does not match source triggers")

    rules = []
    trigger_projection = {}
    steps = {}
    profiles = {}
    budgets = {}
    capabilities = {}
    output_schemas = {}
    transitions = {}
    for trigger, (rule_id, entry_step, edges, terminals) in zip(triggers, topology, strict=True):
        rules.append({"id": rule_id, "entry_step": entry_step})
        trigger_projection[rule_id] = {"event_type": trigger}
        steps[entry_step] = {"type": "llm", "rule": rule_id}
        profiles[entry_step] = source["profile_id"]
        budgets[entry_step] = dict(STRUCTURAL_BUDGETS[playbook_id])
        capabilities[entry_step] = STRUCTURAL_CAPABILITIES[playbook_id]
        output_schemas[entry_step] = STRUCTURAL_OUTPUT_SCHEMAS[playbook_id]
        transitions[entry_step] = dict(edges)
        for terminal_step, outcome in terminals.items():
            steps[terminal_step] = {"type": "terminal", "rule": rule_id, "outcome": outcome}

    return {
        "id": source["id"],
        "purpose": STRUCTURAL_PURPOSES[playbook_id],
        "scope": _scope_projection(source["scope"]),
        "rules": rules,
        "triggers": trigger_projection,
        "steps": steps,
        "profiles": profiles,
        "budgets": budgets,
        "capabilities": capabilities,
        "output_schemas": output_schemas,
        "transitions": transitions,
    }


def _actual_artifact_projection(artifact: Mapping[str, Any]) -> dict[str, Any]:
    rules = artifact.get("rules", ())
    steps = artifact.get("steps", {})
    return {
        "id": artifact.get("id"),
        "purpose": artifact.get("purpose"),
        "scope": artifact.get("scope"),
        "rules": [
            {"id": rule.get("id"), "entry_step": rule.get("entry_step")} for rule in rules
        ],
        "triggers": {rule.get("id"): rule.get("trigger") for rule in rules},
        "steps": {
            step_id: {
                key: step.get(key)
                for key in ("type", "rule", "outcome")
                if key in step
            }
            for step_id, step in steps.items()
        },
        "profiles": {
            step_id: step.get("profile_id")
            for step_id, step in steps.items()
            if step.get("type") == "llm"
        },
        "budgets": {
            step_id: step.get("budget")
            for step_id, step in steps.items()
            if step.get("type") == "llm"
        },
        "capabilities": {
            step_id: _capability_projection(step.get("tool_use", {}))
            for step_id, step in steps.items()
            if step.get("type") == "llm"
        },
        "output_schemas": {
            step_id: step.get("output_schema")
            for step_id, step in steps.items()
            if step.get("type") == "llm"
        },
        "transitions": {
            step_id: step.get("transitions")
            for step_id, step in steps.items()
            if step.get("type") == "llm"
        },
    }


def structural_parity(
    playbook_id: str,
    *,
    artifact: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(v1, v2)`` source and artifact projections for one LLM playbook."""
    fixture = REPO_ROOT / "tests" / "fixtures" / "playbooks" / "v2" / playbook_id
    shipped = _frontmatter((REPO_ROOT / STRUCTURAL_SOURCES[playbook_id]).read_text("utf-8"))
    reviewed = _frontmatter((fixture / "source.md").read_text("utf-8"))
    artifact_data = artifact
    if artifact_data is None:
        artifact_data = json.loads((fixture / "artifact.json").read_text("utf-8"))

    def source_projection(data: Mapping[str, Any]) -> dict[str, Any]:
        return {key: data.get(key) for key in STRUCTURAL_SOURCE_FIELDS}

    return (
        {
            "source": source_projection(shipped),
            "artifact": _expected_artifact_projection(playbook_id, shipped),
        },
        {
            "source": source_projection(reviewed),
            "artifact": _actual_artifact_projection(artifact_data),
        },
    )


# ---------------------------------------------------------------------------
# T-12 — the recorded report
# ---------------------------------------------------------------------------

#: How each closed rationale is exercised.  An id with no entry here is a stale
#: waiver; the suite fails on it (T-10 assertion 5).
RATIONALE_COVERAGE: Mapping[str, str] = {
    "run-per-rule": (
        "test_run_per_rule_is_exercised — task-completed-with-branch-and-pr starts two "
        "independent V2 runs for the one event V1 covered with a single run row"
    ),
    "rule-failure-isolation": (
        "test_rule_failure_isolation_is_exercised — a raising first rule ends V1's whole "
        "dispatch and leaves V2's sibling rule running"
    ),
    "loop-frame-shape": (
        "test_loop_frame_shape_is_exercised — task-completed-with-downstream iterates a "
        "typed foreach frame plus a body step where V1 had one node, with identical "
        "per-iteration commands"
    ),
    "unassigned-ref-rejected": (
        "test_unassigned_ref_rejected_is_exercised — V1 substitutes an unassigned "
        "reference to None/'' where V2's resolver raises"
    ),
    "terminal-vocabulary": (
        "test_terminal_vocabulary_is_the_narrow_expected_difference — compare() maps V2's "
        "timed_out/cancelled onto V1's failed"
    ),
    "null-template-part-rendered": (
        "the corpus itself — every task.completed event with a null pr_url produces this "
        "finding on per-task-review--create-review"
    ),
}

#: What this corpus provably does *not* cover (§4.5), recorded in the report so
#: the bound on the evidence travels with it.
COVERAGE_LIMITS: tuple[str, ...] = (
    (
        "Per-run command comparison covers the deterministic playbooks only "
        f"({', '.join(DETERMINISTIC_PLAYBOOKS)}); "
        f"{', '.join(STRUCTURAL_ONLY_PLAYBOOKS)} are compared structurally "
        "(source metadata plus artifact rules, triggers, step topology, profiles, budgets, "
        "capabilities, output schemas, and transitions) "
        "because their V1 behaviour is an LLM call and is not reproducible."
    ),
    (
        "Agreement is proved over the events in tests/fixtures/playbooks/v2/events/, one per "
        "(rule, guard-outcome) pair of the deterministic playbook, not over all possible events."
    ),
    (
        "Past the shadow binding frontier the V2 arm is projected from the artifact with the "
        "engine's own resolver, taking each boundary outcome from the same scripted oracle the "
        "V1 arm answers with; test_projection_agrees_with_the_engine_at_the_shadow_frontier "
        "pins the projection to the engine wherever shadow resolves."
    ),
)

#: §3.5.1, as applied here.  Recorded in the report because a canonicalisation
#: rule is part of what a reader has to trust.
CANONICALISATION_RULES: tuple[str, ...] = (
    "sensitive arguments are replaced with the contract's redaction marker, key retained",
    "ids the oracle mints during an observation become <gen:N> in first-appearance order, per arm",
    "no contract marks an argument free_text, so whitespace normalisation is a no-op",
    "loop body step ids fold into their foreach parent; step ids compare as <rule>-<title>",
    "bound results project onto the declared result-model fields both arms share",
)


def build_parity_report(
    observations: Sequence[tuple[ShadowObservation, ShadowObservation, Sequence[Any]]],
    *,
    artifact_sha256: str,
) -> dict[str, Any]:
    """The record `playbook_cutover_report` reads (§3.7's ``parity`` block)."""
    identical = expected = unexplained = 0
    per_event = []
    for v1, _v2, findings in observations:
        classes = [finding.classification for finding in findings]
        if not classes:
            identical += 1
        elif "unexplained" in classes:
            unexplained += 1
        else:
            expected += 1
        per_event.append(
            {
                "event_id": v1.event_id,
                "event_type": v1.event_type,
                "rules_selected": list(v1.rules_selected),
                "commands": len(v1.commands),
                "findings": [
                    {
                        "field": finding.field,
                        "classification": finding.classification,
                        "rationale_id": finding.rationale_id,
                    }
                    for finding in findings
                ],
            }
        )
    return {
        "suite": "tests/test_playbook_shadow_parity.py",
        "artifact_sha256": artifact_sha256,
        "v1_source": "tests/fixtures/playbooks/v1/default-pipeline.md",
        "corpus": "tests/fixtures/playbooks/v2/events/",
        "observations": len(observations),
        "identical": identical,
        "expected": expected,
        "unexplained": unexplained,
        "deterministic_playbooks": list(DETERMINISTIC_PLAYBOOKS),
        "structural_only_playbooks": list(STRUCTURAL_ONLY_PLAYBOOKS),
        "canonicalisation": list(CANONICALISATION_RULES),
        "expected_differences": dict(sorted(RATIONALE_COVERAGE.items())),
        "coverage_limits": list(COVERAGE_LIMITS),
        "events": per_event,
    }


async def collect_observations() -> list[tuple[ShadowObservation, ShadowObservation, Any]]:
    """Run both arms over the whole corpus and compare, in corpus order."""
    from src.playbooks.migration import compare

    artifact = load_v2_artifact()
    collected = []
    for case in load_corpus():
        v1 = await run_v1_arm(case, artifact)
        v2, _dispatch, _projection = await run_v2_arm(case, artifact)
        collected.append((v1, v2, compare(v1, v2)))
    return collected


async def record_parity_report() -> dict[str, Any]:
    """Write ``parity-report.json``.  Only ``--parity-record`` calls this."""
    from src.playbooks.definition import artifact_sha256

    observations = await collect_observations()
    report = build_parity_report(observations, artifact_sha256=artifact_sha256(load_v2_artifact()))
    PARITY_REPORT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report
