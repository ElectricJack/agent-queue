"""The render profile: what produced the pixels, hashed once and quoted by name.

``ObjectLoopStartArgs.render_profile_sha256`` and the ``variation`` formula both
demand a render-profile digest, and nothing defined one — so a loop could not
be started from a real ``matter_render`` job.  This module defines it as a
canonical document and its hash, recorded on the job result so a start packet
can quote a value AQ computed rather than one an operator asserted.

The profile is the *preset that rendered the evidence*, not the candidate: the
candidate's own identity is already ``candidate_sha256`` and its rig is already
``rig_sha256``.  What remains unaccounted for — and what silently changes
between two otherwise identical captures — is the editor build, the capture
adapter, the GPU lease, the view set with each view's resolution, the frames
the adapter held for the camera to settle, and the VT readiness counters that
say the detail was actually there.  Two captures of the same candidate on the
same preset therefore share a digest, and any change to any of those inputs
changes it.

Stability is structural, not incidental.  The document is built from named
fields only, so run ids, request ids, timestamps, absolute paths and the
adapter's own verbose protocol transcript — none of which describe the preset —
cannot leak into it, and the canonical form is AQ's single sorted-key,
no-whitespace JSON encoding.
"""

from __future__ import annotations

import hashlib
from typing import Any

from src.object_loop.artifacts import canonical_bytes

RENDER_PROFILE_VERSION = 1
PRESET = "matter_render"

#: Readiness counters that prove the GPU had finished when the frame was
#: captured: the VT queue, its dirty and rejected pages, unmatched tokens and
#: the parts whose BLAS/visibility refinement was still outstanding.  These are
#: outcomes of the same preset converging, not per-run jitter, so they belong in
#: the identity; ``stable_camera_frames`` does not (see ``hold_frames``).
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


def _view_profile(view: dict) -> dict:
    """The per-view half of the profile: geometry and GPU readiness."""
    result = (view.get("capture") or {}).get("result") or {}
    image = view.get("image") or result.get("image") or {}
    readiness = result.get("readiness") or {}
    return {
        "resolution": {
            "width": image.get("width"),
            "height": image.get("height"),
        },
        "format": image.get("format"),
        "vt_budget": {field: readiness.get(field) for field in VT_BUDGET_FIELDS},
    }


def render_profile(contract: dict, capture: dict | None) -> dict:
    """Build the canonical render-profile document for one ``matter_render`` job.

    *contract* is the job's own contract (``adapter_sha256``, ``gpu_id`` and the
    admitted ``capture_expected``); *capture* is the retained capture receipt
    ``retain_capture`` verified.  The document never reads the run directory,
    so it is identical whether it is computed at completion or read back from
    the immutable result.
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
        "admitted_sha256": {
            "candidate_sha256": expected.get("candidate_sha256"),
            "rig_sha256": expected.get("rig_sha256"),
        },
        "readiness": receipt.get("readiness"),
        "views": {name: _view_profile(views[name]) for name in sorted(views)},
    }


def render_profile_sha256(contract: dict, capture: dict | None) -> str:
    """The digest a start packet quotes as ``render_profile_sha256``."""
    return hashlib.sha256(canonical_bytes(render_profile(contract, capture))).hexdigest()


def with_digest(profile: dict) -> dict[str, Any]:
    """The profile as recorded on a job result: the document plus its digest."""
    return {**profile, "sha256": hashlib.sha256(canonical_bytes(profile)).hexdigest()}