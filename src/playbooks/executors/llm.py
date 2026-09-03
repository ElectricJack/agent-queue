"""Budgeted, schema-bound V2 LLM step executors."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
from typing import Any, ClassVar

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from src.commands.authorization import authorize_command, denial_result
from src.commands.principal import check_delegation
from src.llm.spec import LLMCallSpec
from src.llm.types import TokenUsage
from src.playbooks.definition import LLM_RESERVED_OUTCOMES, LlmStep, _outcome_enum
from src.playbooks.executors.base import (
    ENGINE_RESERVED_OUTCOMES,
    ExecutionMode,
    ExecutorResult,
    StepContext,
    StepControl,
    project_step_receipt,
)
from src.playbooks.receipts import idempotency_key
from src.profiles.capabilities import capability_policy_for


def _attempt_key(ctx: StepContext) -> str:
    return idempotency_key(
        ctx.run_id, ctx.step_id, -1 if ctx.iteration_index is None else ctx.iteration_index, ctx.attempt
    )


def _operation(step: LlmStep, ctx: StepContext) -> str:
    try:
        resolved = ctx.services.llm.resolve(_spec(step, ctx))
        return f"llm:{step.profile_id}/{resolved.model}"
    except Exception:  # noqa: BLE001 - a receipt must never expose resolution details
        return f"llm:{step.profile_id}"


def _spec(step: LlmStep, ctx: StepContext) -> LLMCallSpec:
    return LLMCallSpec(
        intelligence_class=step.profile_id,
        max_tokens=step.budget.max_output_tokens,
        caller=f"playbook:{ctx.artifact.id}:{ctx.step_id}",
    )


def _parse_and_validate(text: str, step: LlmStep) -> dict[str, Any]:
    """Read precisely one JSON object; prose cannot participate in branching."""
    start = text.find("{")
    if start < 0:
        raise ValueError("response contains no JSON object")
    value, _end = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(value, dict):
        raise ValueError("response JSON is not an object")
    Draft202012Validator(step.output_schema).validate(value)
    return value


def _usage_breaches(step: LlmStep, usage: TokenUsage) -> bool:
    return (
        usage.output_tokens > step.budget.max_output_tokens
        or (
            step.budget.max_total_tokens is not None
            and usage.total > step.budget.max_total_tokens
        )
    )


def _result(
    step: LlmStep,
    ctx: StepContext,
    *,
    outcome: str,
    usage: TokenUsage | None = None,
    value: dict[str, Any] | None = None,
    diagnostics: tuple[str, ...] = (),
    control: StepControl = StepControl.ADVANCE,
) -> ExecutorResult:
    prompt_digest = hashlib.sha256(_prompt(step, ctx).encode()).hexdigest()
    _ignored_inputs, receipt_result = project_step_receipt(
        {}, value or {}, run_id=ctx.run_id
    )
    return ExecutorResult(
        control=control,
        outcome=outcome,
        value=value if step.save_result_as else None,
        usage=usage,
        idempotency_key=_attempt_key(ctx),
        receipt_inputs={"prompt_digest": prompt_digest},
        receipt_result=receipt_result,
        operation=_operation(step, ctx),
        diagnostics=diagnostics,
    )


def _prompt(step: LlmStep, ctx: StepContext) -> str:
    """Render only the typed prompt expression; receipt code stores its digest."""
    from src.playbooks.expressions import resolve_value

    prompt = str(resolve_value(step.prompt, ctx.scope))
    if not ctx.inputs:
        return prompt
    return f"{prompt}\n\nInputs:\n{json.dumps(dict(ctx.inputs), sort_keys=True, default=str)}"


def _resume_state(
    prompt: str, ctx: StepContext
) -> tuple[list[dict[str, Any]], int, TokenUsage | None]:
    """Rebuild only completed turns belonging to this exact attempt."""
    iteration = -1 if ctx.iteration_index is None else ctx.iteration_index
    turns = sorted(
        (
            turn
            for turn in ctx.llm_turns
            if turn.get("step_id") == ctx.step_id
            and int(turn.get("iteration", -1)) == iteration
            and int(turn.get("attempt", 1)) == ctx.attempt
        ),
        key=lambda turn: int(turn["turn_index"]),
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    usage: TokenUsage | None = None
    for turn in turns:
        messages.extend(dict(message) for message in turn.get("transcript_delta", ()))
        raw_usage = turn.get("usage") or {}
        turn_usage = TokenUsage(
            input_tokens=int(raw_usage.get("input_tokens", 0)),
            output_tokens=int(raw_usage.get("output_tokens", 0)),
            reported=bool(raw_usage.get("reported", False)),
        )
        usage = turn_usage if usage is None else usage + turn_usage
    next_turn_index = int(turns[-1]["turn_index"]) + 1 if turns else 0
    return messages, next_turn_index, usage


def _published_tools(step: LlmStep, ctx: StepContext) -> list[dict[str, Any]]:
    """The model-visible projection; dispatch remains the authorization gate."""
    if not step.tool_use.enabled:
        return []
    tools: list[dict[str, Any]] = []
    for name in step.tool_use.aq_commands:
        registration = ctx.services.contracts.get(name)
        resolver = ctx.services.resolver
        if registration is None or resolver is None:
            continue
        if not authorize_command(
            name,
            ctx.principal,
            resolver=resolver,
            mode=ctx.services.authorization_mode,
        ).allowed:
            continue
        tools.append(
            {
                "name": name,
                "description": registration.contract.presentation.summary,
                "input_schema": registration.contract.execution.args_model.model_json_schema(),
            }
        )
    return tools


async def _profile_principal(step: LlmStep, ctx: StepContext) -> tuple[Any | None, tuple[str, ...]]:
    """Resolve the named profile from the server authority and only narrow.

    The database is the authority that command dispatch itself uses to resolve
    a profile policy.  An executor must never derive this policy from an LLM
    class, the artifact fingerprint, or prompt data; all three are insufficient
    to prove that the requested profile remains a subset of the caller.
    """
    get_profile = getattr(ctx.services.db, "get_profile", None)
    if not callable(get_profile):
        return None, ("profile authority unavailable",)
    try:
        profile = await get_profile(step.profile_id)
        if profile is None:
            return None, ("named profile unavailable",)
        resolver = ctx.services.resolver
        plugin_command_names = (
            resolver.plugin_command_names() if resolver is not None else frozenset()
        )
        policy = capability_policy_for(profile, plugin_command_names=plugin_command_names)
        parent_policy = ctx.principal.policy
        widening = check_delegation(parent_policy, policy)
    except Exception:  # noqa: BLE001 - authority failures must fail closed
        return None, ("profile authority unavailable",)
    if widening:
        return None, ("named profile exceeds invoking principal",)
    return replace(
        ctx.principal.narrow(policy, reason=f"llm-profile:{step.profile_id}"),
        profile_id=step.profile_id,
    ), ()


class LiveLlmExecutor:
    """Execute one profile-selected call under all declared hard budgets."""

    step_type: ClassVar[str] = "llm"
    mode: ClassVar[ExecutionMode] = ExecutionMode.LIVE
    no_side_effects: ClassVar[bool] = False

    async def execute(self, step: LlmStep, ctx: StepContext) -> ExecutorResult:
        if ctx.cancel_requested:
            return _result(step, ctx, outcome="cancelled", diagnostics=("cancellation requested",))
        if ctx.services.llm is None:
            return _result(step, ctx, outcome="unavailable", diagnostics=("LLM client unavailable",))

        principal, diagnostics = await _profile_principal(step, ctx)
        if principal is None:
            return _result(step, ctx, outcome="unauthorized", diagnostics=diagnostics)
        ctx = replace(ctx, principal=principal)

        spec = _spec(step, ctx)
        try:
            resolved = ctx.services.llm.resolve(spec)
            provider = ctx.services.llm._provider_for(resolved)
        except Exception:  # noqa: BLE001 - profiles/providers may reload between boundaries
            return _result(step, ctx, outcome="unavailable", diagnostics=("profile unavailable",))

        if step.budget.max_total_tokens is not None and not provider.reports_usage:
            return _result(
                step,
                ctx,
                outcome="budget_exceeded",
                usage=TokenUsage(),
                diagnostics=("provider does not report usage",),
            )

        try:
            prompt = _prompt(step, ctx)
            retries = step.retry.max_attempts if step.retry is not None else 1
            messages, next_turn_index, usage = _resume_state(prompt, ctx)
            calls = next_turn_index
            for retry_index in range(retries + 1):
                if calls >= step.budget.max_calls:
                    return _result(step, ctx, outcome="budget_exceeded", usage=usage)
                tools = _published_tools(step, ctx)
                denied = False

                async def dispatch_tool(name: str, args: dict[str, Any]) -> Any:
                    nonlocal denied
                    resolver = ctx.services.resolver
                    if resolver is None or not authorize_command(
                        name,
                        ctx.principal,
                        resolver=resolver,
                        mode=ctx.services.authorization_mode,
                    ).allowed:
                        denied = True
                        return denial_result(name)
                    registration = ctx.services.contracts.get(name)
                    if registration is None:
                        return {"success": False, "error": "tool is not contracted"}
                    try:
                        validated = registration.contract.execution.args_model.model_validate(args)
                        response = await registration.invoke(validated, ctx.principal)
                        return response.value.model_dump()
                    except Exception as exc:  # noqa: BLE001 - tool loop receives a safe error result
                        return {"success": False, "error": type(exc).__name__}

                async with asyncio.timeout(step.budget.timeout_seconds):
                    if tools:
                        run = await ctx.services.llm.run_tools(
                            messages,
                            tools,
                            dispatch_tool,
                            spec=spec,
                            max_turns=step.budget.max_calls - calls,
                            on_tool_turn=ctx.on_tool_turn,
                            initial_turn_index=next_turn_index,
                        )
                        response_text, call_usage = run.text, run.usage or TokenUsage()
                        calls += run.turns
                    else:
                        response = await ctx.services.llm.complete(prompt, spec=spec)
                        response_text, call_usage = response.text, response.usage or TokenUsage()
                        calls += 1
                usage = call_usage if usage is None else usage + call_usage
                if tools and run.stopped_by == "interrupted":
                    return _result(
                        step,
                        ctx,
                        outcome="operator_decision_required",
                        usage=usage,
                        diagnostics=("LLM call interrupted",),
                        control=StepControl.OPERATOR_DECISION,
                    )
                if denied:
                    return _result(step, ctx, outcome="unauthorized", usage=usage)
                if step.budget.max_total_tokens is not None and not usage.reported:
                    return _result(
                        step,
                        ctx,
                        outcome="budget_exceeded",
                        usage=usage,
                        diagnostics=("provider did not report usage",),
                    )
                if _usage_breaches(step, usage):
                    return _result(step, ctx, outcome="budget_exceeded", usage=usage)
                try:
                    value = _parse_and_validate(response_text, step)
                except (ValueError, ValidationError):
                    if retry_index == retries:
                        return _result(step, ctx, outcome="invalid_output", usage=usage)
                    prompt = f"{prompt}\nReturn only JSON that validates against the declared schema."
                    continue
                outcome = value.get(step.outcome_field) if step.outcome_field else "completed"
                if not isinstance(outcome, str) or outcome not in step.transitions:
                    return _result(step, ctx, outcome="invalid_output", usage=usage)
                return _result(step, ctx, outcome=outcome, usage=usage, value=value)
        except TimeoutError:
            return _result(step, ctx, outcome="timed_out")
        except asyncio.CancelledError:
            return _result(step, ctx, outcome="cancelled")
        except Exception as exc:  # noqa: BLE001 - provider errors are a typed outcome
            return _result(step, ctx, outcome="provider_error", diagnostics=(type(exc).__name__,))


class SymbolicLlmExecutor:
    """Expose declared LLM branches without calling a provider."""

    step_type: ClassVar[str] = "llm"
    mode: ClassVar[ExecutionMode] = ExecutionMode.DRY_RUN
    no_side_effects: ClassVar[bool] = True

    async def execute(self, step: LlmStep, ctx: StepContext) -> ExecutorResult:
        enum = _outcome_enum(step) or []
        possible = tuple(
            sorted(set(enum) | (set(step.transitions) & (LLM_RESERVED_OUTCOMES | ENGINE_RESERVED_OUTCOMES)))
        )
        return ExecutorResult(
            control=StepControl.UNRESOLVED,
            outcome="unavailable",
            idempotency_key=_attempt_key(ctx),
            operation=_operation(step, ctx),
            diagnostics=("LLM invocation is symbolic in this mode",),
            possible_outcomes=possible,
        )


__all__ = ["LiveLlmExecutor", "SymbolicLlmExecutor"]
