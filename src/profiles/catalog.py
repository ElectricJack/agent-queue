"""Derive the worker profiles this host can actually run.

Vault profile markdown is user-owned once seeded, but a profile is not a
promise that its matching CLI can run on this host.  This module derives the
worker set, observes the existing secret-free login probes, and records which
profiles are safe defaults.  Rerunning ``aq install`` after login, removal, or
repair thus refreshes routing eligibility without erasing operator choices.

**The worker set is a derivation, not an inventory.**  A pool session is
welded at launch to one intelligence class — it runs a CLI whose model cannot
change between claims — so pull-mode work genuinely needs one worker identity
per class.  Writing those out by hand meant the same role, rules and
capabilities copied once per rung, drifting the moment anyone edited one.

Here they are a cross-product instead: for each harness whose CLI is installed
and authenticated, one rung per intelligence class that has a slice for that
harness's provider.  Two rules that used to be hand-maintained now fall out of
the class files themselves — ``astra-*`` carries only OpenAI slices, so it
yields no Claude rung, and nothing from the deep tier upward carries a
``google`` slice, so Gemini yields only the cheap rungs.

What lands in the vault is a *stub*: ``id``, ``extends: worker-<harness>``,
and its own ``## Config``.  Everything else is inherited at sync time
(:mod:`src.profiles.inheritance`), so editing a template updates every rung
with nothing to regenerate, and the only thing a rung file holds is the state
that is genuinely its own — its class, and the pool bounds an operator sets
with ``aq pool scale``.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.install.logins import AuthProbe, login_instructions, provider_logins
from src.install.platform import PlatformFacts
from src.profiles.parser import parse_profile
from src.profiles.retired_defaults import retired_default_ids

logger = logging.getLogger(__name__)

ACTIVATION_FILENAME = "profile-activation.json"
ACTIVATION_SCHEMA_VERSION = 1
#: Harness id -> (display title, the intelligence-class provider key it reads).
#: The provider key is what decides which classes yield a rung for this
#: harness: a class with no slice for it is not runnable there and is simply
#: not derived.
#: Gemini ships no template, and so derives no rungs: nothing from the deep
#: tier upward carries a ``google`` slice, so its rungs would be the two cheap
#: classes and nothing else.  Adding ``("gemini", "Gemini", "google")`` here
#: and a ``worker-gemini`` template is all it would take — the classes already
#: say which rungs that yields.
WORKER_PROVIDERS: tuple[tuple[str, str, str], ...] = (
    ("claude", "Claude", "anthropic"),
    ("codex", "Codex", "openai"),
)
SUPPORTED_PROVIDER_IDS = frozenset(provider for provider, _, _ in WORKER_PROVIDERS)

#: Id of the template each harness's rungs extend.  Shipped in
#: ``src/profiles/defaults/`` and seeded by ``vault.ensure_default_profiles``;
#: carries ``template: true``, so it is never a profile in its own right.
def template_profile_id(harness: str) -> str:
    return f"worker-{harness}"


#: A rung's id.  ``<class>-<harness>`` rather than ``worker-<class>-<harness>``
#: because the class is the interesting half and operators were already
#: writing pool profiles by that name.
def rung_profile_id(class_id: str, harness: str) -> str:
    return f"{class_id}-{harness}"


@dataclass(frozen=True, slots=True)
class CatalogProfile:
    id: str
    provider_id: str
    harness: str
    intelligence_class: str
    name: str
    source_profile_id: str


@dataclass(frozen=True, slots=True)
class ProfileActivation:
    profile: CatalogProfile
    active: bool
    reason: str
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.profile.provider_id,
            "harness": self.profile.harness,
            "active": self.active,
            "reason": self.reason,
            "remediation": self.remediation,
        }


def shipped_profile_catalog(
    providers: Iterable[str] = tuple(sorted(SUPPORTED_PROVIDER_IDS)),
    classes: Mapping[str, Any] | None = None,
) -> tuple[CatalogProfile, ...]:
    """Every (class x harness) rung, in stable order.

    ``classes`` is an intelligence-class snapshot; ``None`` means "the classes
    this release ships", which is what a caller asking *is this id one of
    ours* wants.  :func:`refresh_catalog_profiles` passes the **vault's**
    classes instead, so an operator's own class yields rungs too.

    A class yields a rung for a harness only when it has a slice naming a
    model for that harness's provider.  Nothing here special-cases a provider:
    the class files decide.
    """
    from src.intelligence_classes import resolve_class

    snapshot = _shipped_classes() if classes is None else classes
    titles = {harness: title for harness, title, _ in WORKER_PROVIDERS}
    provider_keys = {harness: provider for harness, _, provider in WORKER_PROVIDERS}
    entries: list[CatalogProfile] = []
    for harness in providers:
        if harness not in SUPPORTED_PROVIDER_IDS:
            raise ValueError(f"unsupported profile-catalog provider: {harness}")
        provider = provider_keys[harness]
        for class_id in sorted(snapshot):
            cls = snapshot[class_id]
            # The Codex CLI reads its own slice where a class defines one;
            # everywhere else the API slice is the answer.
            slice_ = resolve_class(cls, "codex") if harness == "codex" else {}
            if not slice_:
                slice_ = resolve_class(cls, provider)
            if not str(slice_.get("model") or "").strip():
                continue
            entries.append(
                CatalogProfile(
                    id=rung_profile_id(class_id, harness), provider_id=harness,
                    harness=harness, intelligence_class=class_id,
                    name=f"{titles[harness]} · {_class_title(cls, class_id)}",
                    source_profile_id=template_profile_id(harness),
                )
            )
    return tuple(entries)


def _class_title(cls: Any, class_id: str) -> str:
    return str(getattr(cls, "name", "") or "").strip() or class_id


def _shipped_classes() -> Mapping[str, Any]:
    """Parse the bundled class files without needing a vault or a daemon."""
    from src.intelligence_classes import _parse_file

    root = Path(__file__).resolve().parent.parent / "prompts" / "default_intelligence_classes"
    out: dict[str, Any] = {}
    for path in sorted(root.glob("*.md")):
        try:
            cls = _parse_file(str(path))
        except OSError:
            continue
        if cls is not None:
            out[cls.id] = cls
    return out


def evaluate_catalog(
    probes: Iterable[AuthProbe], *, facts: PlatformFacts | None = None, interactive: bool = True,
    classes: Mapping[str, Any] | None = None,
) -> tuple[ProfileActivation, ...]:
    """Map provider installation/readiness facts onto every derived rung."""
    del facts  # The installer admits supported macOS/WSL hosts before this point.
    by_provider = {probe.provider_id: probe for probe in probes}
    logins = {login.provider_id: login for login in provider_logins()}
    result: list[ProfileActivation] = []
    for profile in shipped_profile_catalog(classes=classes):
        probe = by_provider.get(profile.provider_id)
        login = logins[profile.provider_id]
        if probe is None or not probe.installed:
            result.append(ProfileActivation(
                profile, False, f"{login.title} is not installed",
                f"Install {login.title}, then rerun `aq install` to refresh profile eligibility. {login.docs_url}",
            ))
        elif not probe.authenticated:
            result.append(ProfileActivation(
                profile, False, f"{login.title} is installed but not authenticated",
                login_instructions(login, probe, interactive=interactive),
            ))
        else:
            result.append(ProfileActivation(profile, True, "provider CLI is installed and authenticated"))
    return tuple(result)


def activation_path(data_dir: str | os.PathLike[str]) -> Path:
    return Path(data_dir) / "vault" / ACTIVATION_FILENAME


def active_catalog_profile_ids(data_dir: str | os.PathLike[str]) -> set[str] | None:
    """Read active derived profile ids, or ``None`` when there is no usable record.

    ``None`` means *no evidence*, and callers read it as "do not filter".  A
    record written before the worker set changed shape is no evidence either:
    its ids name rungs that no longer exist, so treating it as authoritative
    would mark every current rung ineligible and leave projects with no
    default profile at all.  Detect that by the record's **key** set rather
    than its active subset — a record where every rung is legitimately
    inactive still names ids we recognise.
    """
    try:
        payload = json.loads(activation_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping) or payload.get("schema_version") != ACTIVATION_SCHEMA_VERSION:
        return None
    profiles = payload.get("profiles")
    if not isinstance(profiles, Mapping):
        return None
    known = {profile.id for profile in shipped_profile_catalog()}
    if known and not (known & {str(profile_id) for profile_id in profiles}):
        logger.info(
            "profile activation record predates the current worker set; "
            "treating eligibility as unknown until `aq install` refreshes it"
        )
        return None
    return {
        str(profile_id) for profile_id, detail in profiles.items()
        if isinstance(detail, Mapping) and detail.get("active") is True
    }


def derive_rungs_from_vault(data_dir: str | os.PathLike[str]) -> dict[str, list[str]]:
    """Seed any missing rung for the templates this vault has.  No probes.

    The daemon runs this at startup so adding an intelligence class yields its
    workers without waiting for the next ``aq install``.  Login state is not
    consulted here — that is what the activation record written by
    :func:`refresh_catalog_profiles` is for, and it keeps gating *routing*.
    Seeding a rung whose CLI turns out to be missing costs a file and a
    visible quarantine, which is how every other unusable profile behaves;
    refusing to seed until an installer has run costs the operator every
    worker on the box.
    """
    from src.intelligence_classes import load_intelligence_classes

    root = Path(data_dir)
    profiles_root = root / "vault" / "agent-types"
    templates = {
        harness
        for harness, _, _ in WORKER_PROVIDERS
        if (profiles_root / template_profile_id(harness) / "profile.md").is_file()
    }
    if not templates:
        return {"created": [], "skipped": [], "retired": [], "duplicates": []}
    catalog = shipped_profile_catalog(
        providers=sorted(templates),
        classes=load_intelligence_classes(str(root)) or None,
    )
    return _seed_rungs(root, catalog)


def refresh_catalog_profiles(
    data_dir: str | os.PathLike[str], probes: Iterable[AuthProbe], *,
    facts: PlatformFacts | None = None, interactive: bool = True,
) -> dict[str, list[str]]:
    """Persist eligibility and seed the rungs this host can run.

    The class snapshot comes from the **vault**, so an operator's own class
    yields rungs exactly like a shipped one.  Existing files are left intact;
    an inactive rung is retained if it is already present, while the
    activation record removes it from default routing until its provider is
    repaired.  Seeding never deletes: a rung whose class has disappeared is
    retired by :mod:`src.profiles.class_retirement`.
    """
    from src.intelligence_classes import load_intelligence_classes

    activations = evaluate_catalog(
        probes, facts=facts, interactive=interactive,
        classes=load_intelligence_classes(str(data_dir)) or None,
    )
    root = Path(data_dir)
    result = _seed_rungs(
        root,
        [activation.profile for activation in activations if activation.active],
    )
    result["inactive"] = [
        activation.profile.id for activation in activations if not activation.active
    ]
    _write_activation(
        activation_path(root),
        {"schema_version": ACTIVATION_SCHEMA_VERSION,
         "profiles": {activation.profile.id: activation.to_dict() for activation in activations}},
    )
    return result


def _seed_rungs(root: Path, profiles: Iterable[CatalogProfile]) -> dict[str, list[str]]:
    """Write a stub for each rung that has none.  Never overwrites, never deletes."""
    profiles_root = root / "vault" / "agent-types"
    retired = retired_default_ids(str(root))
    existing_routes = _existing_profile_routes(profiles_root)
    result: dict[str, list[str]] = {
        "created": [], "skipped": [], "inactive": [], "retired": [], "duplicates": []
    }
    for profile in profiles:
        if profile.id in retired:
            result["retired"].append(profile.id)
            continue
        destination = profiles_root / profile.id / "profile.md"
        if destination.exists():
            result["skipped"].append(profile.id)
            continue
        route = (profile.harness, profile.intelligence_class)
        if route in existing_routes:
            result["duplicates"].append(profile.id)
            logger.info(
                "catalog profile %s not seeded; %s already serves harness=%s default_class=%s",
                profile.id, existing_routes[route], profile.harness, profile.intelligence_class,
            )
            continue
        if not _ensure_template(profiles_root, profile.source_profile_id):
            result["skipped"].append(profile.id)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(render_rung_stub(profile), encoding="utf-8")
        result["created"].append(profile.id)
        existing_routes[route] = profile.id
        logger.info(
            "derived worker rung created: %s (harness=%s default_class=%s)",
            profile.id, profile.harness, profile.intelligence_class,
        )
    return result


def _ensure_template(profiles_root: Path, template_id: str) -> bool:
    """Make sure *template_id* is in the vault, seeding the shipped one if not.

    A stub resolves ``extends`` against the **vault**, so a rung whose
    template is missing would sync as a worker with no role and no
    capabilities.  ``vault.ensure_default_profiles`` normally puts it there
    first; seeding it here too means ``aq install`` on a vault that predates
    templates still produces working rungs instead of a directory of stubs
    pointing at nothing.  An existing template is never overwritten.
    """
    destination = profiles_root / template_id / "profile.md"
    if destination.is_file():
        return True
    source = Path(__file__).with_name("defaults") / template_id / "profile.md"
    if not source.is_file():
        logger.warning("no shipped template %s; its rungs cannot be seeded", template_id)
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    logger.info("worker template seeded: %s", template_id)
    return True


def render_rung_stub(profile: CatalogProfile) -> str:
    """The whole file a derived rung gets.

    Deliberately this short.  Everything a reader might expect to find here —
    role, rules, capabilities, harness, workspaces — is inherited from the
    template named by ``extends`` and resolved at sync time, so there is no
    copy of it to drift and nothing to regenerate when the template changes.
    ``lifecycle: task`` is the default because it preserves push behaviour: a
    rung becomes a pool only when an operator sizes it with ``aq pool``.
    """
    config = {"default_class": profile.intelligence_class, "lifecycle": "task"}
    return (
        "---\n"
        f"id: {profile.id}\n"
        f'name: "{profile.name}"\n'
        f"extends: {profile.source_profile_id}\n"
        "tags: [profile, agent-type, worker, derived]\n"
        "---\n"
        "\n"
        f"# {profile.name}\n"
        "\n"
        f"Derived worker: the `{profile.source_profile_id}` template run at\n"
        f"intelligence class `{profile.intelligence_class}`. Role, rules, capabilities\n"
        "and harness are inherited — edit the template to change them for every rung\n"
        "at once. This file holds only what is this rung's own: its class, and the\n"
        "pool state `aq pool` writes here.\n"
        "\n"
        "## Config\n"
        "```json\n"
        + json.dumps(config, indent=2)
        + "\n```\n"
    )


def _stage_profile_ids() -> frozenset[str]:
    """Profiles written for one pipeline stage, not as a generic worker.

    Imported lazily: :mod:`src.profiles.default_selection` imports this
    module, so a module-level import would be a cycle.
    """
    from src.profiles.default_selection import (
        EXCLUDED_PROFILE_IDS,
        SPECIAL_PURPOSE_PROFILE_IDS,
    )

    return frozenset(SPECIAL_PURPOSE_PROFILE_IDS | EXCLUDED_PROFILE_IDS | {"pr-merger"})


def worker_route(
    profile_id: str,
    *,
    harness: object,
    default_class: object,
    lifecycle: object = "task",
    template: bool = False,
    read_only: bool = False,
) -> tuple[str, str] | None:
    """The ``(harness, class)`` execution route *profile_id* is a worker on, or ``None``.

    "Worker" means a profile that could stand in for the generic worker on
    that route — the thing a derived rung is, and the thing a pool sizes.
    Both places that compare profiles by route use this one answer:
    :func:`_existing_profile_routes` (does an operator's worker already serve
    the route a rung would be seeded on?) and doctor's
    ``pools.task_lifecycle_shadow`` (does a push profile duplicate a pool's
    route?).  Counting a non-worker as a route was a bug in both — it
    suppressed real rungs, and it warned about the shipped stages on every
    fresh install once ``aq install`` made every active rung a pool.

    A profile is **not** a worker route when:

    - it names no harness or no class — it has no route at all;
    - it is a ``template: true`` file, which is not runnable;
    - its lifecycle is ``named`` — a resident session is not a task route;
    - it is ``read_only`` — a profile that may not write cannot do worker
      work, whatever class it reads at;
    - its id is a shipped stage profile — ``triage`` running ``fast-low`` on
      Claude says nothing about whether this host wants a ``fast-low-claude``
      worker.  An operator's own single-purpose profile that is writable and
      carries none of the signals above is still a route, because nothing
      structural distinguishes it from a hand-authored worker.
    """
    harness_id = str(harness or "").strip()
    class_id = str(default_class or "").strip()
    if not (harness_id and class_id):
        return None
    if template or read_only:
        return None
    if str(lifecycle or "task").strip() not in {"task", "pool"}:
        return None
    if profile_id in _stage_profile_ids():
        return None
    return (harness_id, class_id)


def _existing_profile_routes(profiles_root: Path) -> dict[tuple[str, str], str]:
    """Return the first valid *worker* profile for each harness/class route.

    A derived rung is a convenience, never a reason to shadow an operator's
    already-working worker.  A task-lifecycle worker counts: it still proves
    that this host has an authored harness/class choice.  What does and does
    not count as a worker is :func:`worker_route`'s decision.
    """
    routes: dict[tuple[str, str], str] = {}
    if not profiles_root.is_dir():
        return routes
    for path in sorted(profiles_root.glob("*/profile.md")):
        try:
            parsed = parse_profile(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if not parsed.is_valid:
            continue
        profile_id = parsed.frontmatter.id or path.parent.name
        route = worker_route(
            profile_id,
            harness=parsed.config.get("harness"),
            default_class=parsed.config.get("default_class"),
            lifecycle=parsed.config.get("lifecycle"),
            template=parsed.frontmatter.template,
            read_only=parsed.config.get("read_only") is True,
        )
        if route is not None:
            routes.setdefault(route, profile_id)
    return routes


def _write_activation(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".profile-activation-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


__all__ = [
    "ACTIVATION_FILENAME",
    "ACTIVATION_SCHEMA_VERSION",
    "WORKER_PROVIDERS",
    "CatalogProfile",
    "ProfileActivation",
    "activation_path",
    "active_catalog_profile_ids",
    "evaluate_catalog",
    "refresh_catalog_profiles",
    "shipped_profile_catalog",
    "worker_route",
]
