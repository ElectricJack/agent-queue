"""Git facts for the reduced train, without delivery records or state mutations.

A visit reuses the fetched delivery snapshot and the completion source locator.
Overlapping readers share only fetches started after their own arrival.
Only immutable Git facts survive visits, keyed by repository and source/target
OIDs. Task identity, authorization and remote freshness are checked separately
at use; a cached fact never authorizes a close or publication on its own.
Legacy consumers remain on delivery_truth until the protocol cutover.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace

from src.git.manager import GitError, GitManager, is_valid_git_oid
from src.integration.delivery_truth import (
    MISSING_PROVENANCE,
    DeliveryEvidence,
    DeliveryRequest,
    DeliverySnapshot,
    DeliveryState,
    delivery_snapshot,
)
from src.integration.provenance import CompletionIdentity, GitProvenance
from src.integration.selection_metrics import selection_count

logger = logging.getLogger(__name__)


class SharedFetchCancelled(RuntimeError):
    """The fetching reader was cancelled; overlapping readers can retry a later visit."""


class _HistoricalPatchFailure(GitError):
    """A historical comparison failed after all other patch candidates were tried."""

    def __init__(self, error: GitError | OSError):
        super().__init__("historical patch comparison is unavailable")
        self.error = error


def _failure_message(exc: Exception) -> str:
    """Describe failures without exposing Git stderr, paths or repository content."""
    message = str(exc)
    # Only fixed messages can leave the observer. Git stderr and exception
    # arguments may contain credentials, filenames, commit bodies or file data.
    known = (
        "whole-source proof requires an exact base OID",
        "provenance requires a full lowercase Git OID",
        "source base is not its ancestor",
        "source does not resolve to the exact commit",
        "invalid whole-source patch id",
        "git command stdin exceeds the bounded input limit",
        "unsupported provenance record",
        "provenance record does not retain its exact source",
        "provenance marker changes the source tree",
        "invalid completion artifact/claim evidence",
        "completion ref has a different immutable identity",
        "completion requires exact project/repository/task/generation",
    )
    if message in known:
        return message
    if isinstance(exc, KeyError):
        return "required provenance field is missing"
    if isinstance(exc, OSError):
        return "Git store or process is unavailable"
    if isinstance(exc, GitError):
        if "Not a valid commit name" in message or "bad object" in message:
            return "Git commit object is unavailable"
        if "timed out" in message:
            return "Git command timed out"
        return "Git command failed (untrusted detail omitted)"
    return "invalid delivery input (untrusted detail omitted)"


@dataclass
class _PairFacts:
    ancestor: bool | None = None
    trailers: dict[str, bool] = field(default_factory=dict)
    patches: dict[str, bool] = field(default_factory=dict)
    equal_tree: bool | None = None
    merge_noop: bool | None = None


@dataclass(frozen=True)
class GitDeliveryEvidence(DeliveryEvidence):
    source_base: str | None = None
    error_detail: str | None = None


@dataclass
class _SharedFetch:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    fetch_starts: int = 0
    snapshot: GitTruthSnapshot | None = None
    error: Exception | None = None
    readers: int = 0


class GitTruth:
    """Bounded OID-pair cache and nonblocking fetch backoff for repository visits."""

    def __init__(
        self, git: GitManager, *, cache_limit: int = 2048,
        retry_delay: float = 1, max_retry_delay: float = 60,
        clock: Callable[[], float] = time.monotonic,
        share_fetches: bool = False,
    ):
        if cache_limit < 1 or retry_delay <= 0 or max_retry_delay < retry_delay:
            raise ValueError("invalid Git truth cache/backoff limits")
        self.git = git
        self.cache_limit = cache_limit
        self.retry_delay, self.max_retry_delay, self.clock = retry_delay, max_retry_delay, clock
        self._cache: OrderedDict[tuple[str, str, str, str], _PairFacts] = OrderedDict()
        # Validated immutable records and whole proofs must be cached too:
        # caching ancestry alone still launches eight processes per warm task.
        self._completions: OrderedDict[tuple, dict] = OrderedDict()
        self._proofs: OrderedDict[tuple, GitDeliveryEvidence] = OrderedDict()
        self._objects: OrderedDict[tuple, str] = OrderedDict()
        self._failures: dict[tuple[str, str], tuple[float, float, str]] = {}
        self.share_fetches = share_fetches
        self._fetches: dict[tuple[str, str, str, str], _SharedFetch] = {}

    async def snapshot(
        self, store: str, *, project_id: str, repository_id: str,
        repository_url: str, target_ref: str,
    ) -> GitTruthSnapshot:
        """Share only an observation whose fetch starts after this caller arrives.

        A caller arriving during a fetch waits for the next one; readers queued
        behind that fetch share its successor, bounding a burst to two fetches.
        There is no age-based cache. The lock covers fetch and ref capture.
        Unexpected failures propagate to overlapping readers without retrying.
        If the fetching caller is cancelled, only it receives CancelledError;
        other readers receive SharedFetchCancelled. A later visit can try again.
        """
        if not self.share_fetches:
            return await self._snapshot(store, project_id=project_id,
                repository_id=repository_id, repository_url=repository_url, target_ref=target_ref)
        key = str(store), project_id, repository_id, repository_url
        shared = self._fetches.setdefault(key, _SharedFetch())
        arrival = shared.fetch_starts
        shared.readers += 1
        try:
            async with shared.lock:
                if shared.error is not None:
                    raise shared.error
                if shared.fetch_starts <= arrival:
                    shared.fetch_starts += 1
                    try:
                        shared.snapshot = await self._snapshot(store, project_id=project_id,
                            repository_id=repository_id, repository_url=repository_url,
                            target_ref=target_ref)
                    except asyncio.CancelledError:
                        shared.error = SharedFetchCancelled("shared fetch was cancelled")
                        raise
                    except Exception as exc:
                        shared.error = exc
                        raise
                else:
                    selection_count("shared_fetch_hits")
                assert shared.snapshot is not None
                return shared.snapshot.for_target(target_ref)
        finally:
            shared.readers -= 1
            if not shared.readers:
                del self._fetches[key]

    async def _snapshot(
        self, store: str, *, project_id: str, repository_id: str,
        repository_url: str, target_ref: str,
    ) -> GitTruthSnapshot:
        key = repository_id, repository_url
        failure = self._failures.get(key)
        if failure is not None and self.clock() < failure[0]:
            return GitTruthSnapshot(self, DeliverySnapshot(
                self.git, str(store), project_id, repository_id, repository_url,
                target_ref, None, {}, error=failure[2],
            ), retry_at=failure[0])
        selection_count("fetch_starts")
        observation = await delivery_snapshot(
            self.git, store, project_id=project_id, repository_id=repository_id,
            repository_url=repository_url, target_ref=target_ref,
        )
        retry_at = None
        if observation.error and observation.error.startswith("snapshot_git_error:"):
            delay = min(failure[1] * 2 if failure else self.retry_delay, self.max_retry_delay)
            retry_at = self.clock() + delay
            self._failures[key] = retry_at, delay, observation.error
        else:
            self._failures.pop(key, None)
        return GitTruthSnapshot(self, observation, retry_at=retry_at)

    def _pair(self, snapshot: DeliverySnapshot, source_oid: str) -> _PairFacts:
        key = snapshot.repository_id, snapshot.repository_url, source_oid, snapshot.target_oid
        facts = self._cache.setdefault(key, _PairFacts())
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_limit:
            self._cache.popitem(last=False)
        return facts

    def _remember(self, cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > self.cache_limit:
            cache.popitem(last=False)
        return value

    def _record_key(self, snapshot: DeliverySnapshot, identity: CompletionIdentity) -> tuple:
        return (snapshot.store, snapshot.repository_id, snapshot.repository_url, identity, tuple(
            snapshot.source_heads.get(ref) for ref in identity.read_refs
        ))

    async def _completion(self, snapshot: DeliverySnapshot, identity: CompletionIdentity):
        """Validate once per pinned metadata OID and exact completion identity.

        Ref absence and failed validation are never cached. Retargets, new
        generations, repositories and stores cannot consume an earlier record.
        """
        key = self._record_key(snapshot, identity)
        if key in self._completions:
            selection_count("completion_cache_hits")
            self._completions.move_to_end(key)
            return deepcopy(self._completions[key])
        selection_count("completion_cache_misses")
        record = await GitProvenance(self.git, snapshot.store,
                                    repository_url=snapshot.repository_url).read_completion(
            identity, refs=snapshot.source_heads,
        )
        if record is not None:
            self._remember(self._completions, key, deepcopy(record))
        return record

    async def exact(self, snapshot: DeliverySnapshot, oid: str, *, cached_only=False) -> bool:
        key = snapshot.store, snapshot.repository_id, snapshot.repository_url, oid
        if key not in self._objects:
            selection_count("object_cache_misses")
            if cached_only:
                return False
            await GitProvenance(self.git, snapshot.store,
                                repository_url=snapshot.repository_url).exact(oid)
            self._remember(self._objects, key, oid)
        else:
            selection_count("object_cache_hits")
            self._objects.move_to_end(key)
        return True


@dataclass(frozen=True)
class GitTruthSnapshot:
    truth: GitTruth
    observation: DeliverySnapshot
    retry_at: float | None = None
    #: Diagnostics can consume only proofs populated by a background reader.
    cached_only: bool = False

    @property
    def target_oid(self) -> str | None:
        return self.observation.target_oid

    def for_target(self, target_ref: str) -> GitTruthSnapshot:
        """Another target from the same fetched repository, without a second fetch."""
        if not target_ref.startswith("refs/heads/"):
            raise ValueError("target must be a full branch ref")
        observation = self.observation
        oid = observation.source_heads.get(
            "refs/remotes/origin/" + target_ref.removeprefix("refs/heads/")
        )
        return replace(self, observation=replace(
            observation, target_ref=target_ref, target_oid=oid,
            error=observation.error if observation.error not in {None, "missing_target"} else (
                None if is_valid_git_oid(oid) else "missing_target"
            ),
        ))

    async def is_delivered(
        self, request: DeliveryRequest, *, source_base: str | None = None
    ) -> GitDeliveryEvidence:
        return await is_delivered(self, request, source_base=source_base)

    async def read_completion(self, identity: CompletionIdentity):
        return await self.truth._completion(self.observation, identity)

    async def evaluate_many(self, requests):
        return {request.task_id: await self.is_delivered(request) for request in requests}

    async def is_fresh(self, **kwargs):
        return await self.observation.is_fresh(**kwargs)

    @property
    def error(self):
        return self.observation.error

    async def contains_source(self, task_id: str, source_oid: str, source_base: str) -> bool | None:
        """Whole frozen input proof, for batches without a mutable task projection.

        The caller separately revalidates ordinary identity and authorization.
        None is an unavailable observation and never permission to collect.
        """
        observed = self.observation
        if observed.error or not all(is_valid_git_oid(oid) for oid in (
            source_oid, source_base, observed.target_oid,
        )):
            return None
        try:
            facts = self.truth._pair(observed, source_oid)
            if facts.ancestor is None:
                facts.ancestor = await observed.git.ais_ancestor(
                    observed.store, source_oid, observed.target_oid, strict=True,
                )
            if facts.ancestor is None:
                return None
            if facts.ancestor:
                return True
            identity = f"{task_id}@{source_oid}"
            if identity not in facts.trailers:
                facts.trailers[identity] = bool(await observed.git.alog_grep_trailer(
                    observed.store, observed.target_oid, "AQ-Source", identity,
                ))
            if facts.trailers[identity]:
                return True
            patch_error = None
            if source_base not in facts.patches:
                try:
                    facts.patches[source_base] = await _whole_patch(
                        observed, source_oid, source_base, observed.target_oid,
                    )
                except _HistoricalPatchFailure as exc:
                    patch_error = exc.error
            if facts.patches.get(source_base):
                return True
            if facts.equal_tree is None:
                facts.equal_tree = await _equal_tree(observed, source_oid, observed.target_oid)
            if not facts.equal_tree:
                if facts.merge_noop is None:
                    facts.merge_noop = await merge_noop(
                        observed, source_oid, observed.target_oid)
                if facts.merge_noop:
                    return True
                if patch_error is not None:
                    raise patch_error
            return facts.equal_tree
        except (GitError, OSError, ValueError):
            return None

    async def usable(
        self, evidence: GitDeliveryEvidence, current_request: DeliveryRequest,
        *, current_source_base: str | None = None,
    ) -> bool:
        """Caller reloads ordinary task inputs under its lock before acting.

        A ref retarget, reopen, claim/status change or source-base change cannot
        consume an earlier proof, even when its immutable Git facts are cached.
        """
        return (
            evidence.request == current_request
            and evidence.source_base == current_source_base
            and evidence.target_oid == self.target_oid
            and (current_request.project_id, current_request.repository_id) == (
                self.observation.project_id, self.observation.repository_id,
            )
            and current_request.task_status == "COMPLETED"
            and await self.observation.is_fresh(target_ref=current_request.target_ref)
        )


async def _run(snapshot: DeliverySnapshot, *args: str) -> str:
    result = await snapshot.git.arun_git_result(
        ["--no-replace-objects", "--literal-pathspecs", *args], cwd=snapshot.store,
    )
    if result.returncode:
        raise GitError(result.stderr or "Git truth observation failed")
    return result.stdout


async def _diff_paths(snapshot: DeliverySnapshot, base: str, head: str) -> list[str]:
    return (await _run(snapshot, "diff", "--no-renames", "--name-only", "-z",
                       base, head, "--")).split("\0")[:-1]


@dataclass
class _GeneratedPaths:
    """Classify exact paths using attributes from the immutable target, once each."""

    snapshot: DeliverySnapshot
    known: dict[str, bool] = field(default_factory=dict)

    async def select(self, paths: Sequence[str]) -> set[str]:
        missing = sorted(set(paths) - self.known.keys())
        if missing:
            result = await self.snapshot.git.arun_git_result(
                ["--no-replace-objects", "check-attr", f"--source={self.snapshot.target_oid}",
                 "-z", "--stdin", "merge"],
                cwd=self.snapshot.store, stdin="\0".join(missing) + "\0",
            )
            if result.returncode:
                raise GitError(result.stderr or "generated attribute probe failed")
            fields = result.stdout.split("\0")[:-1]
            if len(fields) != 3 * len(missing) or any(
                fields[i] != path or fields[i + 1] != "merge"
                for i, path in zip(range(0, len(fields), 3), missing, strict=True)
            ):
                raise GitError("invalid generated attribute result")
            self.known.update(
                (fields[i], fields[i + 2] == "aq-generated")
                for i in range(0, len(fields), 3)
            )
        return {path for path in paths if self.known[path]}


async def _whole_patch(
    snapshot: DeliverySnapshot, source: str, base: str, target: str,
) -> bool:
    """Compare the whole intended change with complete reachable target changes.

    A matching *source commit* proves too little for a multi-commit task. The
    sole source patch is base..source. On the target, compare whole commit
    changes (including squash merges) and ranges spanning rewritten commits.
    Each range ends at a reachable commit, so a later revert preserves delivery.
    """
    provenance = GitProvenance(snapshot.git, snapshot.store, repository_url=snapshot.repository_url)
    await provenance.exact(base)
    if not await provenance.ancestor(base, source):
        raise GitError("source base is not its ancestor")
    generated = _GeneratedPaths(snapshot)

    async def patch_id(start: str, head: str, paths: list[str]) -> str | None:
        excluded = await generated.select(paths)
        if excluded:
            return await snapshot.git.apatch_id(
                snapshot.store, start, head, exclude_paths=sorted(excluded),
            )
        return await snapshot.git.apatch_id(snapshot.store, start, head)

    paths = await _diff_paths(snapshot, base, source)
    patch = await patch_id(base, source, paths)
    if patch is None:
        return False
    paths = [path for path in paths if not generated.known[path]]
    common_base = await provenance.ancestor(base, target)
    # Restrict expensive diff probes to commits touching source paths. Read
    # actual object parents with %P; path-limited rev-list rewrites parents.
    history = await _run(snapshot, "log", "--full-history", "--topo-order", "--reverse",
                         "--format=%H %P", target, *(("^" + base,) if common_base else ()),
                         "--", *paths)
    starts = {base} if common_base else set()
    first_error = None
    probes = 0
    for row in history.splitlines():
        head, *parents = row.split()
        after_base = common_base and await provenance.ancestor(base, head)
        # Probe historical commit changes, but accumulate ranges only after
        # the recorded source base. This avoids all-pairs diffs across years
        # of unrelated history on a frequently edited source path.
        candidates = set(parents) | (starts if after_base else set())
        for start in candidates:
            if start == head or not await provenance.ancestor(start, head):
                continue
            probes += 1
            if probes > HISTORICAL_PATCH_PROBE_LIMIT:
                # Every probe so far was negative: the search answers "not
                # found" (pending), never unknown, so a long-lived source
                # cannot block forever. A failed probe stays unavailable.
                if first_error is not None:
                    raise _HistoricalPatchFailure(first_error) from first_error
                return False
            try:
                candidate = await patch_id(start, head, await _diff_paths(snapshot, start, head))
            except (GitError, OSError) as exc:
                # An optional historical probe cannot invalidate the retained
                # source or prevent a later patch/tree from proving delivery.
                if first_error is None:
                    first_error = exc
                continue
            if candidate == patch:
                return True
        if after_base:
            for parent in parents:
                if await provenance.ancestor(base, parent):
                    starts.add(parent)
    if first_error is not None:
        raise _HistoricalPatchFailure(first_error) from first_error
    return False


#: Patch-id comparisons one whole-source proof may run. The range search is
#: quadratic in the commits after the source base that touch the source's
#: paths; an old task on a hot path (2026-10-06: tests/selection_catalogue.json)
#: ran for over 120 s each and kept the root visit past its timeout. Spent, the
#: search answers negative unless a probe failed, and the cheaper no-op merge
#: and tree proofs still run. History is bounded to base..target when the base
#: is on the target: delivered work can only follow its base.
HISTORICAL_PATCH_PROBE_LIMIT = 150


async def merge_noop(
    snapshot: DeliverySnapshot, source: str, target: str, *, target_tree: str | None = None,
) -> bool:
    """Merging the exact source changes only the target's generated artifacts.

    Every change the source makes relative to the merge base is already in the
    target, whatever commits carried it (squash, cherry-pick, re-delivery). A
    non-generated conflict or difference is no proof. Read attributes from the
    pinned target, so neither checkout state nor source edits can hide missing work.
    """
    result = await snapshot.git.arun_git_result(
        ["--no-replace-objects", "merge-tree", "--write-tree", "--name-only", "-z",
         target, source],
        cwd=snapshot.store,
    )
    if result.returncode not in {0, 1}:
        return False
    merged, _, _ = (result.stdout or "").partition("\0")
    if not is_valid_git_oid(merged):
        return False
    generated = _GeneratedPaths(replace(snapshot, target_oid=target))
    if result.returncode:
        # NUL output begins with the tree, conflicted filenames, then an empty
        # field before informational messages. Filenames may contain newlines.
        conflicts = result.stdout.split("\0\0", 1)[0].split("\0")[1:]
        if not conflicts or set(conflicts) - await generated.select(conflicts):
            return False
    if merged == (target_tree or await snapshot.git.atree_sha(snapshot.store, target)):
        return True
    paths = await _diff_paths(snapshot, target, merged)
    return not (set(paths) - await generated.select(paths))


async def _equal_tree(snapshot: DeliverySnapshot, source: str, target: str) -> bool:
    tree = await snapshot.git.atree_sha(snapshot.store, source)
    # Historical equality, rather than only the current tree: subsequent work
    # and reverts do not erase an already delivered unchanged-tree rebase.
    trees = await _run(snapshot, "log", "--format=%T", target, "--")
    return tree in trees.splitlines()


async def is_delivered(
    snapshot: GitTruthSnapshot, request: DeliveryRequest, *, source_base: str | None = None,
) -> GitDeliveryEvidence:
    """Ancestry, exact reachable AQ-Source, whole-source patch, then full tree.

    Bind the current ordinary completion to pinned Git provenance on every call;
    validation is reusable only for its exact immutable metadata OIDs. Full
    proofs additionally bind every request field, source base and target OID.
    Branch names, reported heads, settlements and receipts never locate an
    artifact or satisfy delivery here. Missing input or failed Git is unknown.
    """
    observed = snapshot.observation
    source = None
    proof_key = None

    def answer(
        state: DeliveryState, reason: str, *, error_detail: str | None = None,
    ) -> GitDeliveryEvidence:
        evidence = GitDeliveryEvidence(
            request, state, observed.target_oid, source, reason, source_base, error_detail,
        )
        if proof_key is not None and state is not DeliveryState.UNKNOWN:
            snapshot.truth._remember(snapshot.truth._proofs, proof_key, evidence)
        return evidence

    if (request.project_id, request.repository_id, request.target_ref) != (
        observed.project_id, observed.repository_id, observed.target_ref,
    ):
        return answer(DeliveryState.UNKNOWN, "scope_mismatch")
    if observed.error or not observed.target_oid:
        return answer(DeliveryState.UNKNOWN, observed.error or "missing_target")
    if request.task_status != "COMPLETED":
        return answer(DeliveryState.PENDING, "task_not_completed")
    step = "completion_identity"
    try:
        identity = CompletionIdentity(
            request.project_id, request.repository_id, request.task_id,
            request.completion_id or request.legacy_generation,
        )
        proof_key = (snapshot.truth._record_key(observed, identity),
                     observed.target_oid, request, source_base)
        if proof_key in snapshot.truth._proofs:
            selection_count("delivery_cache_hits")
            snapshot.truth._proofs.move_to_end(proof_key)
            return snapshot.truth._proofs[proof_key]
        selection_count("delivery_cache_misses")
        if snapshot.cached_only:
            return answer(DeliveryState.UNKNOWN, "proof_unavailable")
        provenance = GitProvenance(observed.git, observed.store,
                                   repository_url=observed.repository_url)
        step = "completion_provenance"
        record = await snapshot.read_completion(identity)
        if record is None:
            return answer(DeliveryState.UNKNOWN, MISSING_PROVENANCE)
        step = "source_identity"
        source = record["source_oid"]
        step = "target_object"
        await snapshot.truth.exact(observed, observed.target_oid)
        if (request.completion_commits == () and request.completion_outcome == "pass"
                and source_base is not None and source == source_base):
            # Empty descriptive commits alone prove nothing. The retained
            # generation locates its exact head; the recorded origin is its
            # base. Only their equality and exact target ancestry prove that
            # this passing completion added no work owed to the target.
            step = "no_change_completion"
            if await provenance.ancestor(source, observed.target_oid):
                return answer(DeliveryState.NO_CHANGE, "git_no_change")
            return answer(DeliveryState.PENDING, "no_change_base_not_delivered")
        if not record["artifact"]:
            return answer(DeliveryState.NO_ARTIFACT, "git_no_artifact")
        step = "source_ancestry"
        facts = snapshot.truth._pair(observed, source)
        if facts.ancestor is None:
            facts.ancestor = await provenance.ancestor(source, observed.target_oid)
        if facts.ancestor:
            return answer(DeliveryState.CONTAINED, "ancestor")
        step = "source_trailer"
        trailer = f"{request.task_id}@{source}"
        if trailer not in facts.trailers:
            facts.trailers[trailer] = bool(await observed.git.alog_grep_trailer(
                observed.store, observed.target_oid, "AQ-Source", trailer,
            ))
        if facts.trailers[trailer]:
            return answer(DeliveryState.CONTAINED, "source_trailer")
        patch_error = None
        if source_base is not None:
            step = "source_base"
            if not is_valid_git_oid(source_base):
                raise ValueError("whole-source proof requires an exact base OID")
            if source_base not in facts.patches:
                step = "whole_source_patch"
                try:
                    facts.patches[source_base] = await _whole_patch(
                        observed, source, source_base, observed.target_oid,
                    )
                except _HistoricalPatchFailure as exc:
                    patch_error = exc.error
            if facts.patches.get(source_base):
                return answer(DeliveryState.CONTAINED, "whole_source_patch")
        step = "full_tree"
        if facts.equal_tree is None:
            facts.equal_tree = await _equal_tree(observed, source, observed.target_oid)
        if facts.equal_tree:
            return answer(DeliveryState.CONTAINED, "full_tree")
        step = "merge_noop"
        if facts.merge_noop is None:
            facts.merge_noop = await merge_noop(observed, source, observed.target_oid)
        if facts.merge_noop:
            return answer(DeliveryState.CONTAINED, "merge_noop")
        if patch_error is not None:
            step = "whole_source_patch"
            raise patch_error
        return answer(DeliveryState.PENDING, "source_not_delivered")
    except (GitError, OSError, ValueError, KeyError, TypeError) as exc:
        detail = f"{step}: {type(exc).__name__}: {_failure_message(exc)}"
        logger.warning(
            "Delivery observation failed for task %s on %s: %s",
            request.task_id, request.target_ref, detail,
        )
        return answer(DeliveryState.UNKNOWN, "missing_or_ambiguous_source", error_detail=detail)


async def epic_complete(
    snapshot: GitTruthSnapshot, children: Sequence[DeliveryRequest], *,
    green_oid: str | None, approved_tree: str | None = None,
    review_required: bool = False, held: bool = False,
    source_bases: Mapping[str, str] | None = None,
) -> bool | None:
    """Readiness from ordinary children, exact green head and required tree review.

    None means unavailable Git; False means pending or explicit authorization
    constraints. Check/review producers supply their current trusted verdicts.
    Delivery mutations still revalidate task inputs and the ref with ``usable``.
    """
    if held or any(child.task_status != "COMPLETED" for child in children):
        return False
    if snapshot.observation.error or snapshot.target_oid is None:
        return None
    for child in children:
        proof = await is_delivered(snapshot, child, source_base=(source_bases or {}).get(child.task_id))
        if proof.state == DeliveryState.UNKNOWN:
            return None
        if proof.state not in {
            DeliveryState.CONTAINED, DeliveryState.NO_CHANGE, DeliveryState.NO_ARTIFACT,
        }:
            return False
    if green_oid != snapshot.target_oid:
        return False
    if review_required:
        try:
            tree = await snapshot.truth.git.atree_sha(snapshot.observation.store, snapshot.target_oid)
        except (GitError, OSError, ValueError):
            return None
        if approved_tree != tree:
            return False
    return True


def repair_progress(start_oid: str | None, head_oid: str | None, *, green: bool) -> bool:
    """Trusted green ends repair before counters or head movement are consulted."""
    if green:
        return True
    return bool(is_valid_git_oid(head_oid) and is_valid_git_oid(start_oid) and head_oid != start_oid)


async def commits_added(git: GitManager, store: str, start_oid: str, head_oid: str) -> list[str]:
    """All commits added by repair, including merged commits, from exact OIDs."""
    if not is_valid_git_oid(start_oid) or not is_valid_git_oid(head_oid):
        raise ValueError("repair commits require exact start/head OIDs")
    result = await git.arun_git_result(
        ["--no-replace-objects", "rev-list", "--reverse", f"{start_oid}..{head_oid}"], cwd=store,
    )
    if result.returncode:
        raise GitError(result.stderr or "repair commits are unknown")
    return result.stdout.splitlines()
