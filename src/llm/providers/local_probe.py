"""What a local OpenAI-compatible server is holding right now.

Ollama exposes exactly one piece of process state over HTTP: ``GET /api/ps``
lists the models it currently keeps resident, each with an ``expires_at``. AQ
already asks it that question before a call (:meth:`OpenAIProvider
.is_model_loaded`); the stall ladder asks it a sharper one — *is anything
generating for this session at all?* — because a TUI can claim to be mid-turn
while its model was never loaded (crisp-horizon-90.10, 2026-10-03: an
OpenCode pane repainting its in-turn spinner for 42 minutes against an
``/api/ps`` with no model in it).

Nothing resident is a **positive** answer, not an absence: Ollama answers
``{"models": []}`` when it is up and holding nothing, and 404/garbage/timeout
when it is not Ollama, not up, or not reachable. The first is evidence, the
second is unknown, so this module returns ``None`` for it and never ``False``.
Callers that may take a destructive action on the answer must hold on ``None``.

One ``GET``, no SDK and no credentials: this is a health fact about a local
process, not a completion request, so it must stay reachable from a module that
has no provider client (``src/sessions/provider_liveness.py``) and cheap enough
to run once a poll interval.
"""

from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)

#: Where Ollama listens unless the operator moved it. ``/v1`` is the
#: OpenAI-compatible prefix OpenCode and AQ are configured with; the native API
#: root drops it (:func:`api_root`).
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"

#: A failed probe must never look like a loaded model, so this is generous:
#: any second the operator's box is busy reading a 27B model's residency.
DEFAULT_TIMEOUT_SECONDS = 5.0

#: Hosts whose OpenAI-compatible surface is not a local server AQ can ask.
_VENDOR_HOSTS = ("api.openai.com", "generativelanguage.googleapis.com")


def api_root(base_url: str) -> str:
    """The native API root of an OpenAI-compatible ``base_url``.

    ``http://host:11434/v1`` -> ``http://host:11434``: ``/api/ps`` is Ollama's
    own path, not one under the OpenAI-compatible prefix.
    """
    return (base_url or "").rstrip("/").removesuffix("/v1")


def is_local_base_url(base_url: str) -> bool:
    """Whether *base_url* names something on this box that can be asked.

    A vendor API has no ``/api/ps`` and is never a worker's own model server,
    so a base_url naming one is not a local endpoint however it is spelled.
    """
    url = (base_url or "").strip()
    return bool(url) and not any(host in url for host in _VENDOR_HOSTS)


def running_models(base_url: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> set[str] | None:
    """The models the endpoint reports resident, or ``None`` when unknown.

    ``None`` covers every case that is not Ollama answering: a connection
    refused, a timeout, a non-JSON body, a ``models`` key that is not a list.
    Callers must not read it as "nothing is loaded".
    """
    root = api_root(base_url)
    if not root:
        return None
    request = urllib.request.Request(f"{root}/api/ps", method="GET")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
        models = data["models"] if isinstance(data, dict) else None
        if not isinstance(models, list):
            return None
    except Exception:
        logger.debug("local model probe of %s failed", root, exc_info=True)
        return None
    names = set()
    for entry in models:
        if isinstance(entry, dict) and entry.get("name"):
            names.add(str(entry["name"]))
    return names
