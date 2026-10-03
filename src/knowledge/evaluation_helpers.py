"""Deterministic, provider-neutral knowledge fixture evaluation.

Part of the approved plan
``projects/agent-queue/plans/2026-10-01-aq-work-and-knowledge-records-implementation-plan.md``
(sections 9 and 13): a provider-neutral offline fixture runner for
record/revision/citation selection, forbidden-identity leakage, budget and
duplicate-delivery invariants.

This module is the documented adapter boundary the parallel K08 slice
(vivid-quest-44.1) consumes: it defines the stable identities, budget
accounting, selection shape, delivery ledger and leakage checks that a
rendered ContextBundle must satisfy.  It does NOT import ContextBundle,
does NOT select, rank, summarize or verify records, and does NOT enable any
memory provider.  Plan section 9: "Adapters never rank, summarize or verify
independently."  K08 owns ``src/knowledge/context.py`` / ``budget.py``
selection on top of the same boundary; K09 (vivid-quest-44.2) wires the thin
harness delivery/resume adapters and the same fixture runner.

Design rules implemented here
-----------------------------

- Provider-neutral: identical bundles across worker/supervisor roles and
  across the Claude / Codex / OpenCode / local adapter labels must yield
  identical selection, and the runner enforces that with
  ``cross_adapter_parity``.
- Frozen time: ``prepared_at`` on the bundle is the only wall-clock input;
  the runner never reads the clock (``FrozenClock.now()`` raises).
- Token budget: an exact tokenizer identity preferred; otherwise a
  conservative UTF-8 ``upper_bound_bytes`` count.  Never ``char // 4``
  (plan section 9: "Do not rely on characters/4").
- Transport spy: every ``socket.create_connection`` / ``ssl`` handshake /
  ``urlopen`` is refused and recorded.  Settings alone do not prove offline
  behavior (plan section 13); the runner re-checks this after the adapter
  runs.  A green evaluation has zero recorded calls.
- Synthetic fixtures are labelled: every manifest must declare
  ``source_class`` (``synthetic`` or ``authorized_sanitized``) and
  ``synthetic: true``.  A synthetic fixture may test semantics; it does not
  satisfy a later real-incident quality gate by itself (plan section 13).

Schema versions
---------------

``KNOWLEDGE_FIXTURE_SCHEMA_VERSION = 1`` — stable contract identifiers for
the manifest, bundle, delivery ledger and evaluation report.  Consumers
(K08, K09) bind the version, not the path.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import socket
from dataclasses import dataclass, field
from typing import Any

KNOWLEDGE_FIXTURE_SCHEMA_VERSION = 1
KNOWN_ADAPTERS: tuple[str, ...] = ("claude", "codex", "opencode", "local")
KNOWN_ROLES: tuple[str, ...] = ("worker", "supervisor")
KNOWN_SOURCE_CLASSES: tuple[str, ...] = ("synthetic", "authorized_sanitized")

_ADAPTER_TOKEN_RE = re.compile(
    r"\b(claude\w*|codex|gemini|opencode|gpt\w*|llama\w*|mistral\w*|deepseek\w*)\b",
    re.I,
)


# ---------------------------------------------------------------------------
# Transport spy: offline proof, not a setting
# ---------------------------------------------------------------------------
class TransportSpy:
    """Refuse and record every live transport call.

    Install with ``spy.guard()`` in a ``with`` block:

    >>> spy = TransportSpy()
    >>> with spy.guard():
    ...     ...  # any socket.create_connection / ssl / urlopen call raises
    >>> spy.calls  # == [] for a clean offline run

    A green evaluation run must show ``calls == []``; a red run (an adapter
    attempting a live call) shows the call name and arguments, plus the
    ``RuntimeError`` the adapter saw.  A pass with calls is a fail, and a
    fail with no calls is what the runner asserts.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self._saved: list[tuple[Any, str, Any]] = []

    def _refuse(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        raise RuntimeError(
            f"transport spy: live provider call refused ({name}); offline fixture evaluation"
        )

    def install(self) -> "TransportSpy":
        self._saved.append((socket, "create_connection", socket.create_connection))
        socket.create_connection = lambda *a, **k: self._refuse("socket.create_connection", *a, **k)
        try:
            import ssl

            self._saved.append((ssl, "create_default_context", ssl.create_default_context))
            self._saved.append((ssl, "wrap_socket", ssl.wrap_socket))

            def _ctx_fail(*a: Any, **k: Any) -> Any:
                return self._refuse("ssl.create_default_context", *a, **k)

            def _wrap_fail(*a: Any, **k: Any) -> Any:
                return self._refuse("ssl.wrap_socket", *a, **k)

            ssl.create_default_context = _ctx_fail
            ssl.wrap_socket = _wrap_fail
        except Exception:  # pragma: no cover - ssl is stdlib
            pass
        try:
            import urllib.request

            self._saved.append((urllib.request, "urlopen", urllib.request.urlopen))
            urllib.request.urlopen = lambda *a, **k: self._refuse("urlopen", *a, **k)
        except Exception:  # pragma: no cover - urllib is stdlib
            pass
        return self

    def remove(self) -> None:
        for module, attr, original in reversed(self._saved):
            setattr(module, attr, original)
        self._saved.clear()

    def guard(self) -> "_SpyGuard":
        return _SpyGuard(self)

    @property
    def refused_count(self) -> int:
        return len(self.calls)


class _SpyGuard:
    def __init__(self, spy: TransportSpy) -> None:
        self._spy = spy

    def __enter__(self) -> "TransportSpy":
        self._spy.install()
        return self._spy

    def __exit__(self, *exc: object) -> None:
        self._spy.remove()


# ---------------------------------------------------------------------------
# Budget accounting: exact tokenizer identity or byte upper bound
# ---------------------------------------------------------------------------
class Budget:
    """Deterministic token/byte budget accounting.

    ``token_unit`` is an identity, not a magic:

    - ``"exact:whitespace"`` — one token per whitespace-separated word
      (identity-declared; a real tokenizer would be ``exact:<name>`` and the
      runner would call into it; the runner refuses unknown identities).
    - ``"upper_bound_bytes:utf8"`` — conservative UTF-8 byte count; every
      UTF-8 byte is at least one token, so the byte count is a valid upper
      bound.  This is the documented default.

    ``char // 4`` is refused: it is not an upper bound for multibyte UTF-8
    and it is not an exact tokenizer identity (plan section 9).
    """

    EXACT = "exact"
    UPPER_BOUND_BYTES = "upper_bound_bytes"

    def __init__(
        self,
        token_unit: str = "upper_bound_bytes:utf8",
        max_tokens: int = 4096,
        max_bytes: int = 16384,
    ) -> None:
        self.token_unit = token_unit
        self.max_tokens = int(max_tokens)
        self.max_bytes = int(max_bytes)

    def _split(self, text: str) -> list[str]:
        if self.token_unit.startswith("exact:"):
            name = self.token_unit.split(":", 1)[1]
            if name == "whitespace":
                return text.split()
            raise ValueError(f"unknown exact tokenizer identity: {name!r}")
        if self.token_unit == "upper_bound_bytes:utf8":
            return [text]
        raise ValueError(f"unknown token unit: {self.token_unit!r}")

    def token(self, text: str) -> int:
        """Conservative token count under the declared unit.

        For ``upper_bound_bytes:utf8`` this returns the UTF-8 byte count of
        ``text`` (at least 1 for non-empty text; 0 for empty).  For an exact
        identity it returns the count under that identity.
        """

        if not text:
            return 0
        if self.token_unit.startswith("exact:"):
            return max(1, len(self._split(text)))
        if self.token_unit == "upper_bound_bytes:utf8":
            return max(1, len(text.encode("utf-8")))
        raise ValueError(f"unknown token unit: {self.token_unit!r}")

    def bytes(self, text: str) -> int:
        """UTF-8 byte count — the documented conservative bound."""

        return len(text.encode("utf-8"))

    def fits(self, texts: list[str], reserved_tokens: int = 0, reserved_bytes: int = 0) -> bool:
        total_tokens = sum(self.token(t) for t in texts) + reserved_tokens
        total_bytes = sum(self.bytes(t) for t in texts) + reserved_bytes
        return total_tokens <= self.max_tokens and total_bytes <= self.max_bytes


# ---------------------------------------------------------------------------
# Selection: deterministic, provider-neutral, identity-based
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RecordRef:
    """A stable identity for a knowledge record/revision (K08 pins the same).

    ``record_id`` is the knowledge record; ``revision_id`` pins the exact
    revision whose bytes the selection is about; the pair is a composite
    identity (plan section 9).  The identity is not derived from provider
    labels.
    """

    record_id: str
    revision_id: str
    kind: str

    @property
    def identity(self) -> str:
        return f"{self.record_id}@{self.revision_id}:{self.kind}"


@dataclass
class SelectionItem:
    """One ordered knowledge item in a rendered bundle.

    ``evidence`` / ``authority`` / ``freshness`` labels are required
    metadata (plan section 9).  ``selection_reason`` explains the choice so
    a golden report can be reviewed.  ``content_sha256`` pins the exact
    bytes the selector chose; required in any report where the item is not
    omitted.
    """

    ref: RecordRef
    excerpt: str
    selection_reason: str
    evidence: str
    authority: str
    freshness: str
    content_sha256: str
    omitted: bool = False
    omission_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "record_id": self.ref.record_id,
            "revision_id": self.ref.revision_id,
            "kind": self.ref.kind,
            "excerpt": self.excerpt,
            "selection_reason": self.selection_reason,
            "evidence": self.evidence,
            "authority": self.authority,
            "freshness": self.freshness,
            "content_sha256": self.content_sha256,
        }
        if self.omitted:
            d["omitted"] = True
            d["omission_reason"] = self.omission_reason
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SelectionItem":
        return cls(
            ref=RecordRef(d["record_id"], d["revision_id"], d["kind"]),
            excerpt=d.get("excerpt", ""),
            selection_reason=d.get("selection_reason", ""),
            evidence=d.get("evidence", ""),
            authority=d.get("authority", ""),
            freshness=d.get("freshness", ""),
            content_sha256=d.get("content_sha256", ""),
            omitted=bool(d.get("omitted", False)),
            omission_reason=d.get("omission_reason"),
        )


@dataclass
class Selection:
    """The ordered selection for a bundle: in-scope items first, omissions after.

    Deterministic: two adapters with the same bundle/role/budget/query must
    produce the same ``Selection``.  The runner asserts this on the
    cross-adapter fixture via ``cross_adapter_parity``.
    """

    items: list[SelectionItem] = field(default_factory=list)

    def in_scope(self) -> list[SelectionItem]:
        return [i for i in self.items if not i.omitted]

    def omissions(self) -> list[SelectionItem]:
        return [i for i in self.items if i.omitted]

    def identities(self) -> set[str]:
        return {i.ref.identity for i in self.in_scope()}

    def to_dict(self) -> dict[str, Any]:
        return {"items": [i.to_dict() for i in self.items]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Selection":
        return Selection(items=[SelectionItem.from_dict(x) for x in d.get("items", [])])


# ---------------------------------------------------------------------------
# Deterministic selector: the documented adapter boundary for K08.
#
# K08 (vivid-quest-44.1) drives selection with its own ContextBundle on the
# same boundary.  The selector here is the reference implementation of the
# invariants that every adapter must hold: exact record/revision identity,
# forbidden-identity exclusion, current-eligibility (no stale revision),
# deterministic ordering, budget-aware omissions and cross-adapter parity.
# K08 owns the selection engine; this module exposes the shape so the
# runner can be tested independently.
# ---------------------------------------------------------------------------
def select_records(
    *,
    role: str,
    budget: Budget,
    query: str = "",
    records: list[RecordRef] | None = None,
    current: set[str] | None = None,
    retired: set[str] | None = None,
    forbidden: set[str] | None = None,
    excerpts: dict[str, str] | None = None,
    query_terms: list[str] | None = None,
) -> Selection:
    """Deterministic provider-neutral record/revision selection.

    - Only ``current`` identities with an exact revision are eligible.
    - ``retired`` identities are never selected; they may appear as omissions
      with reason ``retired`` (explicit-read warning, not a selection).
    - ``forbidden`` identities are never selected AND never rendered in the
      report (no excerpt, no content_sha256, no reason string).
    - Order is by identity string (deterministic), not by wall clock.
    - The selection respects the budget: drops happen only in
      ``apply_budget_gate`` (see that function), never here.

    ``excerpts`` maps identity -> exact body bytes the selector chose.  Any
    identity whose excerpt contains a forbidden token gets the forbidden
    identity's exclusion rule applied (its selection_reason and excerpt are
    cleared, and the item is an omission with reason
    ``forbidden-identity-excerpt``).
    """

    terms = set(query_terms if query_terms is not None else _terms(query))
    sel = Selection()
    for ref in sorted(records or [], key=lambda r: r.identity):
        ident = ref.identity
        if ident in (forbidden or set()):
            sel.items.append(_omitted(ref, "forbidden-identity"))
            continue
        if ident not in (current or set()):
            if ident in (retired or set()):
                sel.items.append(_omitted(ref, "retired"))
            else:
                sel.items.append(_omitted(ref, "not-current-eligible"))
            continue
        excerpt = (excerpts or {}).get(ident, "")
        if ident in (forbidden or set()):
            sel.items.append(_omitted(ref, "forbidden-identity-excerpt"))
            continue
        reason = "current-eligible"
        if terms:
            reason = f"current-eligible:query-match[{','.join(sorted(terms))}]"
        sel.items.append(
            SelectionItem(
                ref=ref,
                excerpt=excerpt,
                selection_reason=reason,
                evidence="synthetic-fixture",
                authority="synthetic-authorized",
                freshness="current",
                content_sha256=sha256_text(excerpt) if excerpt else "",
            )
        )
    return sel


def _omitted(ref: RecordRef, reason: str) -> SelectionItem:
    return SelectionItem(
        ref=ref,
        excerpt="",
        selection_reason="",
        evidence="",
        authority="",
        freshness="",
        content_sha256="",
        omitted=True,
        omission_reason=reason,
    )


def _terms(query: str) -> list[str]:
    if not query:
        return []
    return re.findall(r"[a-z0-9_]+", query.lower())


# ---------------------------------------------------------------------------
# Budget gate: whole-item drop, omissions recorded, required content first
# ---------------------------------------------------------------------------
def apply_budget_gate(selection: Selection, budget: Budget, required: set[str]) -> Selection:
    """Drop whole knowledge items until budget holds; record omissions.

    - Never truncates an item mid-byte; an omitted item carries exactly one
      reason: a budget reason or the typed
      ``context.required_over_budget`` diagnostic.
    - Required identities stay if the budget permits; if the required set
      alone exceeds the budget, the runner emits the typed diagnostic
      (plan section 9: "If required non-knowledge content alone exceeds the
      configured input budget, render no knowledge and a typed
      context.required_over_budget diagnostic").
    - Ordering is preserved from the input selection.
    """

    total_tokens = 0
    total_bytes = 0
    items: list[SelectionItem] = []
    required_over = False

    for item in selection.items:
        if item.omitted:
            items.append(item)
            continue
        ident = item.ref.identity
        excerpt = item.excerpt
        t = budget.token(excerpt)
        b = budget.bytes(excerpt)
        fits = (t <= budget.max_tokens - total_tokens) and (b <= budget.max_bytes - total_bytes)
        if ident in required:
            if not fits:
                required_over = True
                items.append(_omitted(item.ref, "context.required_over_budget"))
                continue
            items.append(item)
            total_tokens += t
            total_bytes += b
        else:
            if fits:
                items.append(item)
                total_tokens += t
                total_bytes += b
            else:
                items.append(
                    SelectionItem(
                        ref=item.ref,
                        excerpt=excerpt,
                        selection_reason=item.selection_reason,
                        evidence=item.evidence,
                        authority=item.authority,
                        freshness=item.freshness,
                        content_sha256=item.content_sha256,
                        omitted=True,
                        omission_reason="budget-exceeded",
                    )
                )

    if required_over:
        items.append(
            SelectionItem(
                ref=RecordRef("__required_over_budget__", "n/a", "diagnostic"),
                excerpt="",
                selection_reason="context.required_over_budget",
                evidence="diagnostic",
                authority="none",
                freshness="n/a",
                content_sha256="",
                omitted=True,
                omission_reason="context.required_over_budget",
            )
        )
    return Selection(items=items)


# ---------------------------------------------------------------------------
# Delivery ledger: duplicate-suppression key and state transitions
# ---------------------------------------------------------------------------
@dataclass
class Delivery:
    """A single delivery attempt for a bundle (K08's ``knowledge_context_deliveries`` row).

    ``transport_key`` is the per-bundle unique key the ledger uses to
    suppress duplicate delivery.  ``state`` follows plan section 9:
    ``prepared | delivered | failed | unknown``.  A ``delivered`` delivery
    cannot re-enter; an ``unknown`` delivery can be reconciled to
    ``delivered`` or ``failed`` once the transport is observed.
    """

    delivery_id: str
    bundle_id: str
    transport: str
    transport_key: str
    state: str = "prepared"
    observed_at: str = ""


class DeliveryLedger:
    """Deterministic duplicate-suppression ledger.

    The key is ``transport_key`` (bundle + transport identity).  A second
    ``deliver`` for the same key returns the existing delivery with
    ``newly_delivered=False`` and never re-prepares; a different key is a
    distinct attempt (different harness label, different attempt).

    This mirrors K08's ``knowledge_context_deliveries`` unique
    ``transport_key`` per plan section 9: preparation and observed delivery
    are distinct states, and a duplicate is one row, not two.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, Delivery] = {}
        self._attempts: list[Delivery] = []

    def deliver(self, d: Delivery) -> tuple[Delivery, bool]:
        """Returns ``(delivery, newly_delivered)``.

        A second delivery with the same ``transport_key`` returns the
        existing delivery and ``newly_delivered=False``.
        """

        existing = self._by_key.get(d.transport_key)
        if existing is not None:
            return existing, False
        self._by_key[d.transport_key] = d
        self._attempts.append(d)
        return d, True

    @property
    def attempts(self) -> list[Delivery]:
        return list(self._attempts)

    def get(self, transport_key: str) -> Delivery | None:
        return self._by_key.get(transport_key)

    def reconcile(self, transport_key: str, new_state: str) -> Delivery:
        """Reconcile an ``unknown`` delivery to ``delivered`` or ``failed``."""

        d = self._by_key.get(transport_key)
        if d is None:
            raise KeyError(f"no delivery for transport_key={transport_key!r}")
        if d.state != "unknown":
            raise ValueError(f"cannot reconcile delivery in state {d.state!r}")
        if new_state not in ("delivered", "failed"):
            raise ValueError(f"new_state must be delivered|failed, got {new_state!r}")
        d.state = new_state
        return d

    def as_dict(self) -> dict[str, dict[str, Any]]:
        return {
            k: dataclasses.asdict(v) for k, v in sorted(self._by_key.items())
        }


# ---------------------------------------------------------------------------
# Leakage check: the golden "zero leakage" invariant
# ---------------------------------------------------------------------------
def check_leakage(
    rendered: str,
    forbidden_records: dict[str, dict[str, str]],
    *,
    include_snippets: bool = True,
) -> list[dict[str, Any]]:
    """Return findings for every forbidden identity leaked into ``rendered``.

    ``forbidden_records`` maps identity -> ``{"title": ..., "body": ...,
    "snippet": ...}``.  A finding is one of:

    - ``title`` leaked (raw title string found in rendered)
    - ``body`` leaked (a contiguous run of the forbidden body found)
    - ``snippet`` leaked (a contiguous run of the forbidden snippet found,
      when ``include_snippets`` is True)

    The check is deterministic and never fabricates a positive: an empty
    ``rendered`` never leaks, and an empty forbidden set never finds.
    """

    findings: list[dict[str, Any]] = []
    for ident, parts in sorted(forbidden_records.items()):
        title = (parts.get("title") or "").strip()
        body = (parts.get("body") or "").strip()
        snippet = (parts.get("snippet") or "").strip()
        hit: dict[str, Any] | None = None
        if title and title in rendered:
            hit = {"identity": ident, "leak": "title"}
        elif body and body in rendered:
            hit = {"identity": ident, "leak": "body"}
        elif snippet and include_snippets and snippet in rendered:
            hit = {"identity": ident, "leak": "snippet"}
        if hit is not None:
            findings.append(hit)
    return findings


# ---------------------------------------------------------------------------
# Cross-adapter parity: the provider-neutrality invariant
# ---------------------------------------------------------------------------
def cross_adapter_parity(
    selections_by_adapter: dict[str, Selection],
    adapters: tuple[str, ...] = KNOWN_ADAPTERS,
) -> list[str]:
    """Return problems where adapters disagree on the same selection inputs.

    The inputs (bundle, role, budget, query, identities, forbidden set) are
    fixed by the fixture; the adapters are Claude, Codex, OpenCode and one
    local-model label.  A green fixture has zero problems: every adapter
    selected the same identities, in the same order, with the same omissions
    and the same omission reasons.
    """

    problems: list[str] = []
    reference: tuple[str, Selection] | None = None
    for adapter in adapters:
        sel = selections_by_adapter.get(adapter)
        if sel is None:
            problems.append(f"cross-adapter parity: {adapter} missing")
            continue
        if reference is None:
            reference = (adapter, sel)
            continue
        ref_adapter, ref_sel = reference
        if sel.identities() != ref_sel.identities():
            problems.append(
                f"cross-adapter parity: {adapter} identities != {ref_adapter}: "
                f"{sorted(sel.identities() ^ ref_sel.identities())}"
            )
        ref_items = [(i.ref.identity, i.omitted, i.omission_reason) for i in ref_sel.items]
        my_items = [(i.ref.identity, i.omitted, i.omission_reason) for i in sel.items]
        if my_items != ref_items:
            problems.append(f"cross-adapter parity: {adapter} items differ from {ref_adapter}")
    return problems


def provider_label_leakage(rendered: str, *, allowed: set[str] | None = None) -> list[str]:
    """Find provider/harness labels in rendered content.

    A golden fixture must not have Claude / Codex / OpenCode / local-model
    labels in the rendered bundle unless the fixture explicitly allows its
    own role.  ``allowed`` is the set of labels the fixture permits (empty
    or absent means none allowed).  This is the ``forbidden identities``
    scenario row: a rendered bundle must never leak a provider label the
    fixture did not authorize.
    """

    allowed = allowed or set()
    leaks: list[str] = []
    for m in _ADAPTER_TOKEN_RE.finditer(rendered):
        label = m.group(0).lower()
        if label in allowed:
            continue
        leaks.append(label)
    return sorted(set(leaks))


# ---------------------------------------------------------------------------
# Content hashing: canonical
# ---------------------------------------------------------------------------
def sha256_text(text: str) -> str:
    """SHA-256 hex of the UTF-8 encoding of ``text``."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_dumps(obj: Any) -> str:
    """Canonical JSON for hashing: sorted keys, compact separators, ASCII."""

    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_digest(obj: Any) -> str:
    """SHA-256 of the canonical JSON for ``obj``."""

    return sha256_text(canonical_dumps(obj))


# ---------------------------------------------------------------------------
# Stale claim: a bundle prepared under an old epoch cannot select its prior
# identities at the newer epoch (plan section 9: "A stale claim cannot
# deliver or cite for the next task in a reused slot.")
# ---------------------------------------------------------------------------
def apply_stale_claim(selection: Selection, *, held_identity: str, held_epoch: int, current_epoch: int) -> Selection:
    """Forbid the identity a stale claim previously held.

    When ``current_epoch > held_epoch`` the bundle was prepared under the
    stale claim: ``held_identity`` is omitted with reason ``stale-claim``
    and cannot re-enter the selection, even if it is current-eligible for
    the newer epoch's work.  When the epochs are equal the selection is
    returned unchanged (the claim is not stale).
    """

    if current_epoch <= held_epoch:
        return selection
    items: list[SelectionItem] = []
    replaced = False
    for item in selection.items:
        if item.ref.identity == held_identity and not item.omitted:
            items.append(_omitted(item.ref, "stale-claim"))
            replaced = True
        else:
            items.append(item)
    if not replaced:
        items.append(_omitted(ref_for(held_identity), "stale-claim"))
    return Selection(items=items)


def ref_for(identity: str) -> RecordRef:
    """``record_id@revision_id:kind`` -> ``RecordRef`` (inverse of ``identity``)."""

    rec, rest = identity.split("@", 1)
    rev, kind = rest.split(":", 1)
    return RecordRef(rec, rev, kind)


# ---------------------------------------------------------------------------
# Frozen time
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FrozenClock:
    """Deterministic clock; the runner is forbidden from using ``datetime.now``.

    ``prepared_at`` is an ISO-8601 string pinned by the fixture.  A test
    that asserts frozen-time behavior compares two runs with different
    ``prepared_at`` values and expects the same selection (only the
    rendered timestamp changes); a ``now()`` call is an error.
    """

    prepared_at: str

    def __post_init__(self) -> None:
        if not self.prepared_at:
            raise ValueError("FrozenClock.prepared_at is required")

    @classmethod
    def now(cls) -> "FrozenClock":
        raise RuntimeError(
            "FrozenClock is frozen; use the fixture's prepared_at (plan section 13, "
            "golden tests freeze time/IDs)"
        )


def freeze_time(prepared_at: str = "1970-01-01T00:00:00Z") -> FrozenClock:
    """Build a ``FrozenClock`` from the fixture's ``prepared_at`` (or the default)."""

    return FrozenClock(prepared_at=prepared_at)
