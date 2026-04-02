# Analysis: Is the Reflection System Currently Used?

**Answer: Yes — the reflection system is fully implemented, actively integrated, and in use.**

## Summary

The reflection system is a core part of the Supervisor's action-reflect cycle. It is not dead code or a stub — it is wired end-to-end from configuration through to runtime execution.

## Evidence of Active Usage

### 1. Source Code (`src/reflection.py`)
- **`ReflectionEngine`** class with full implementation: trigger classification, depth determination, prompt building, verdict parsing, token tracking, and circuit breaker logic.
- **`ReflectionVerdict`** dataclass for structured pass/fail results with optional follow-up suggestions.
- Three depth tiers (deep, standard, light) mapped from trigger types and config levels.

### 2. Supervisor Integration (`src/supervisor.py`)
The Supervisor imports and uses the reflection system extensively:

- **Line 37:** `from src.reflection import ReflectionEngine, ReflectionVerdict`
- **Line 147:** `self.reflection = ReflectionEngine(config.supervisor.reflection)` — instantiated on every Supervisor creation.
- **Lines 233–296:** `reflect()` async method — runs a full reflection pass: checks `should_reflect()`, determines depth, builds the prompt, sends it to the LLM, processes tool uses during reflection, records tokens, and parses the verdict.
- **Lines 532–567:** After every tool-use loop in `chat()`, reflection is called. If the verdict is `passed=False`, the Supervisor retries with the reflection feedback (limited to 1 retry via `_reflection_retry_active` flag).
- **Line 677:** Hook completions trigger reflection with `_reflection_trigger="hook.completed"`.
- **Line 798:** Plan splitting triggers reflection with `_reflection_trigger="plan.split"`.

### 3. Configuration (`src/config.py`)
- **`ReflectionConfig`** dataclass (lines 398–421): configurable `level` (off/minimal/moderate/full), `max_depth`, `per_cycle_token_cap`, `hourly_token_circuit_breaker`, `periodic_interval`.
- Default level is `"full"` — meaning reflection is **on by default** for all new installations.
- Parsed from `config.yaml` under `supervisor.reflection` (lines 1176–1184).
- Embedded in `SupervisorConfig` which validates it at startup.

### 4. Tests
- **`tests/test_reflection.py`** — dedicated unit tests for the ReflectionEngine.
- **`tests/test_supervisor.py`** — integration tests covering reflection in the Supervisor's chat loop.
- **`tests/test_config_supervisor.py`** — config validation tests including reflection settings.

### 5. Spec (`specs/reflection.md`)
- Full specification exists documenting the class API, depth determination table, prompt formats, safety controls, and invariants.

## How It Works in Practice

1. User sends a message → Supervisor enters tool-use loop → tools execute.
2. After the loop completes, `reflect()` is called with the appropriate trigger (e.g., `user.request`, `hook.completed`, `plan.split`).
3. `ReflectionEngine.should_reflect()` checks if reflection is enabled and the circuit breaker isn't tripped.
4. `determine_depth()` maps the trigger to deep/standard/light based on the configured level.
5. `build_reflection_prompt()` creates a tier-appropriate prompt.
6. The prompt is sent to the LLM as a system reflection message.
7. The LLM response is parsed into a `ReflectionVerdict`.
8. If `passed=False`, the Supervisor retries the original request with reflection feedback (1 retry max).

## Conclusion

The reflection system is **fully operational** — not a stub, not dead code, not behind a feature flag (though it can be disabled by setting `level: "off"`). It runs on every Supervisor interaction by default at the `"full"` level.
