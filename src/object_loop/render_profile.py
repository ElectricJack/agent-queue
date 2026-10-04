"""The render profile: what produced the pixels, hashed once and quoted by name.

``ObjectLoopStartArgs.render_profile_sha256`` and the ``variation`` formula both
demand a render-profile digest, and nothing defined one — so a loop could not
be started from a real ``matter_render`` job.  This module defines it as a
canonical document and its hash, recorded on the job result so a start packet
can quote a value AQ computed rather than one an operator asserted.

The profile is the *preset that rendered the evidence* and nothing else.  It
names the editor build, the capture adapter, the GPU lease, the rig (with the
view set, the lights the rig fixes and the frame budget it admits) and each
view's resolution — never the object under test.  A digest that folds in the
candidate is not a profile at all: every non-incumbent candidate would render
under a profile no start packet ever pinned, so its score could only ever be
refused as foreign, and no candidate could ever beat the incumbent.  The
candidate's own identity is already ``candidate_sha256`` on the candidate and
on every receipt that scores it.

What the *capture* reported — the VT readiness counters that say the detail was
actually there, and the capture's own readiness state — is an outcome of
rendering this object, not an input to rendering it.  It is recorded beside the
profile (``observed``) and never hashed: a heavier object legitimately queues
more VT work, and hashing that would mint a fresh profile identity per object
for the same reason.  Convergence is still proved, by the retained capture
receipt and by the scorer's own ``ready``/``decoded`` flags.

Stability is structural, not incidental.  The document is built from named
fields only, so run ids, request ids, timestamps, absolute paths, per-run
frame counts and the adapter's own verbose protocol transcript — none of which
describe the preset — cannot leak into it, and the canonical form is AQ's
single sorted-key, no-whitespace JSON encoding.
"""

from __future__ import annotations

import hashlib
from typing import Any

from src.object_loop.artifacts import canonical_bytes

#: Version 2 dropped the candidate identity and the per-capture readiness
#: counters from the hashed document, so a profile digest means "this preset"
#: and two candidates are comparable under it.
RENDER_PROFILE_VERSION = 2
PRESET = "matter_render"

#: Readiness counters the adapter reports per view: the VT queue, its dirty and
#: rejected pages, unmatched tokens and the parts whose BLAS/visibility
#: refinement was still outstanding.  Recorded as an observation of the render,
#: not hashed: they describe what this object produced, and any of them can
#: legitimately differ between two objects under one preset.
VT_BUDGET_FIELDS = (
    "blockers",
    "missing_blas",
    "missing_draws",
    "missing_vt",
    "ready",
    "unmatched_tokens",
    "visible_refinement_pending",
    "visible_sectors_pending",
    "vt_dirty_pages",
    "vt_queue_depth",
    "vt_rejected_variants",
)


class RenderProfileError(ValueError):
    """Raised when a render profile cannot be read off a completed capture."""


def _admitted_views(contract: dict, capture: dict | None) -> tuple[dict, dict]:
    """The admitted expectation and the completed receipt's views, or refuse.

    Shared by the profile and its observations: neither exists without the same
    admission, so neither can quietly describe a capture the job did not admit.
    """
    expected = (contract or {}).get("capture_expected") if isinstance(contract, dict) else None
    if not isinstance(expected, dict):
        raise RenderProfileError("a render profile describes an admitted matter_render capture")
    receipt = (capture or {}).get("receipt") or {}
    views = receipt.get("views")
    if not isinstance(views, dict) or not views:
        raise RenderProfileError("a render profile needs a completed capture receipt")
    admitted = list(expected.get("views") or sorted(views))
    if set(admitted) != set(views):
        raise RenderProfileError("the capture covers a different view set than was admitted")
    return expected, {"receipt": receipt, "views": views}


def _view_settings(view: dict) -> dict:
    """The per-view half of the preset: what the rig asked to be rendered."""
    result = (view.get("capture") or {}).get("result") or {}
    image = view.get("image") or result.get("image") or {}
    return {
        "resolution": {
            "width": image.get("width"),
            "height": image.get("height"),
        },
        "format": image.get("format"),
    }


def render_profile(contract: dict, capture: dict | None) -> dict:
    """Build the canonical render-profile document for one ``matter_render`` job.

    *contract* is the job's own contract (``adapter_sha256``, ``gpu_id`` and the
    admitted ``capture_expected``); *capture* is the retained capture receipt
    ``retain_capture`` verified.  The document never reads the run directory and
    never reads the candidate's own identity, so it is identical whether it is
    computed at completion or read back from the immutable result — and identical
    for every candidate rendered under the same preset.
    """
    expected, admitted = _admitted_views(contract, capture)
    views = admitted["views"]
    return {
        "version": RENDER_PROFILE_VERSION,
        "preset": PRESET,
        "editor_sha256": expected.get("editor_sha256"),
        "adapter_sha256": contract.get("adapter_sha256"),
        "gpu_id": contract.get("gpu_id"),
        # The admitted rig's frame budget, not the frames a run happened to
        # settle on: the capture reports 95, 96 and 106 stable frames for the
        # four views of one render, so the observed count is jitter and hashing
        # it would give every run its own profile identity.
        "hold_frames": expected.get("hold_frames"),
        # The rig subtree digest covers the view set and the lights it fixes,
        # so a candidate that moved either changes the profile.
        "rig_sha256": expected.get("rig_sha256"),
        "views": {name: _view_settings(views[name]) for name in sorted(views)},
    }


def capture_observations(contract: dict, capture: dict | None) -> dict:
    """What this capture reported, recorded beside the profile and never hashed.

    The readiness counters prove the GPU had finished when each frame was
    captured.  They are evidence about this object's render, so they travel with
    the result and are absent from the digest that identifies the preset.
    """
    _, admitted = _admitted_views(contract, capture)
    receipt, views = admitted["receipt"], admitted["views"]
    return {
        "readiness": receipt.get("readiness"),
        "views": {
            name: {
                "vt_budget": {
                    field: ((views[name].get("capture") or {}).get("result") or {})
                    .get("readiness", {}).get(field)
                    for field in VT_BUDGET_FIELDS
                }
            }
            for name in sorted(views)
        },
    }


def render_profile_sha256(contract: dict, capture: dict | None) -> str:
    """The digest a start packet quotes as ``render_profile_sha256``."""
    return hashlib.sha256(canonical_bytes(render_profile(contract, capture))).hexdigest()


def with_digest(profile: dict, observations: dict | None = None) -> dict[str, Any]:
    """The profile as recorded on a job result: the document plus its digest.

    *observations* is carried alongside and deliberately outside the digest: it
    is what this capture reported, not part of the preset it was rendered under.
    """
    record = {**profile}
    if observations is not None:
        record["observed"] = observations
    record["sha256"] = hashlib.sha256(canonical_bytes(profile)).hexdigest()
    return record