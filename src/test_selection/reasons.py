"""Every reason code a selection record may carry.

A record explains each module it proposes, and each fallback it took, with
codes from this module and nothing else: no generated rationale, no file
content.  A code may carry one detail with :func:`with_detail`
(``"mandatory_rule:gamma"``), and the detail is a path, an area id or a rule,
never content.  :data:`ALL_REASONS` pins the set; a test checks it holds
exactly the constants below.
"""

from __future__ import annotations

# ---- the mandatory set M (spec §4.2)
MANDATORY_CHANGED_TEST = "mandatory_changed_test"
MANDATORY_DIRECT_IMPORT = "mandatory_direct_import"
MANDATORY_RULE = "mandatory_rule"
MANDATORY_SCOPED_CONFTEST = "mandatory_scoped_conftest"
MANDATORY_SOURCE_SCAN = "mandatory_source_scan"
MANDATORY_CRITICAL = "mandatory_critical"

# ---- M = U: the whole universe runs
GLOBAL_INVALIDATOR = "global_invalidator"
SNAPSHOT_INCOMPLETE = "snapshot_incomplete"
UNMAPPED_PATH = "unmapped_path"
CATALOGUE_UNUSABLE = "catalogue_unusable"

# ---- the static import closure S
STATIC_IMPACT = "static_impact"
STATIC_UNAVAILABLE = "static_unavailable"

# ---- Jev's per-area judgments J (spec §4.3)
JEV_AFFECTED = "jev_affected"
JEV_UNKNOWN = "jev_unknown"
JEV_CONFIDENT_UNAFFECTED = "jev_confident_unaffected"
JEV_DISABLED = "jev_disabled"
JEV_UNCONFIGURED = "jev_unconfigured"
JEV_MISSING_ANSWER = "jev_missing_answer"
JEV_INVALID = "jev_invalid"
JEV_MODEL_DRIFT = "jev_model_drift"
JEV_CACHED = "jev_cached"

# ---- why a Jev call fell back to F (spec §3.2)
FALLBACK_TIMEOUT = "fallback_timeout"
FALLBACK_BUDGET = "fallback_budget"
FALLBACK_CONNECTION = "fallback_connection"
FALLBACK_HTTP_401 = "fallback_http_401"
FALLBACK_HTTP_422 = "fallback_http_422"
FALLBACK_HTTP_429 = "fallback_http_429"
FALLBACK_HTTP_529 = "fallback_http_529"
FALLBACK_HTTP_OTHER = "fallback_http_other"
PACKING_OVERFLOW = "packing_overflow"

# ---- the caller's request
NARROWING_FLAGS = "narrowing_flags"
EXPLICIT_TARGET = "explicit_target"

ALL_REASONS: frozenset[str] = frozenset(
    {
        MANDATORY_CHANGED_TEST,
        MANDATORY_DIRECT_IMPORT,
        MANDATORY_RULE,
        MANDATORY_SCOPED_CONFTEST,
        MANDATORY_SOURCE_SCAN,
        MANDATORY_CRITICAL,
        GLOBAL_INVALIDATOR,
        SNAPSHOT_INCOMPLETE,
        UNMAPPED_PATH,
        CATALOGUE_UNUSABLE,
        STATIC_IMPACT,
        STATIC_UNAVAILABLE,
        JEV_AFFECTED,
        JEV_UNKNOWN,
        JEV_CONFIDENT_UNAFFECTED,
        JEV_DISABLED,
        JEV_UNCONFIGURED,
        JEV_MISSING_ANSWER,
        JEV_INVALID,
        JEV_MODEL_DRIFT,
        JEV_CACHED,
        FALLBACK_TIMEOUT,
        FALLBACK_BUDGET,
        FALLBACK_CONNECTION,
        FALLBACK_HTTP_401,
        FALLBACK_HTTP_422,
        FALLBACK_HTTP_429,
        FALLBACK_HTTP_529,
        FALLBACK_HTTP_OTHER,
        PACKING_OVERFLOW,
        NARROWING_FLAGS,
        EXPLICIT_TARGET,
    }
)

_SEPARATOR = ":"


def with_detail(code: str, detail: str) -> str:
    """``f"{code}:{detail}"``; *detail* is a path, area id or rule, never content.

    Refuses a code outside :data:`ALL_REASONS` and an empty detail, so a
    record only ever holds pinned codes.
    """
    if code not in ALL_REASONS:
        raise ValueError(f"unknown reason code {code!r}")
    if not detail:
        raise ValueError("reason detail must be non-empty")
    return f"{code}{_SEPARATOR}{detail}"


def code_of(reason: str) -> str:
    """The code of *reason*, without any detail: ``"mandatory_rule:gamma"`` -> ``"mandatory_rule"``."""
    return reason.partition(_SEPARATOR)[0]
