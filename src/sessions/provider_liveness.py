"""Is a session's provider doing any work for it right now?

The stall ladder's other clock is the harness's own record (a transcript's
mtime, or OpenCode's store). This is the *provider's*: a local model server
knows whether it is holding a model for anyone, and a wedged agent is exactly
the case where the two disagree — crisp-horizon-90.10 (2026-10-03) showed an
OpenCode TUI repainting its in-turn spinner for 42 minutes against an Ollama
holding nothing at all, which is a stall with no inference behind it.

The answer is three-valued on purpose. ``False`` is only ever returned for a
*positive* reading — an endpoint that answered ``/api/ps`` with no model in it —
and everything AQ cannot resolve is ``None``:

* a session on a vendor gateway (nothing to ask, and nothing is local);
* a provider key AQ cannot map to a local endpoint;
* an endpoint that is down, is not Ollama, or did not answer.

A caller that may interrupt or restart a worker must hold on ``None``: only
"nothing is resident **and** nothing is in flight" is evidence of a stall, and
"we could not find out" never is.
"""

from __future__ import annotations

import asyncio
import logging

from src.config import normalize_llm_provider
from src.llm.providers.local_probe import (
    DEFAULT_OLLAMA_BASE_URL,
    DEFAULT_TIMEOUT_SECONDS,
    is_local_base_url,
    running_models,
)

logger = logging.getLogger(__name__)

#: Harness-provider keys whose sessions run against a local Ollama. ``ollama``
#: is a harness-level key (it is what ``vault/harnesses/opencode.md`` declares)
#: and normalizes to ``openai``, the id AQ's own credential block uses.
_OLLAMA_KEYS = frozenset({"ollama"})


def local_endpoint(session, config) -> str:
    """The local endpoint this session's provider runs against, or ``""``.

    Two ways to name it, in order:

    * the daemon's own ``llm`` credential block, when this session's provider
      normalizes to the block's provider and its ``base_url`` is not a vendor
      API — that is the case of an operator whose whole box is one Ollama;
    * Ollama's default address, for a session whose provider key is the local
      ``ollama`` one and nothing else names an endpoint.

    A worker whose OpenCode was pointed at a *different* Ollama (``OLLAMA_HOST``
    in its pane environment) is not described by either, and ``""`` is the honest
    answer: the caller falls back to the store's own in-flight record rather than
    asking the wrong server.
    """
    key = str(getattr(session, "llm_provider", "") or "").strip()
    if not key:
        return ""
    llm = getattr(config, "llm", None)
    configured = str(getattr(llm, "base_url", "") or "")
    if (
        configured
        and is_local_base_url(configured)
        and normalize_llm_provider(key)
        == normalize_llm_provider(str(getattr(llm, "provider", "") or ""))
    ):
        return configured
    return DEFAULT_OLLAMA_BASE_URL if key in _OLLAMA_KEYS else ""


async def request_inflight(session, config) -> bool | None:
    """Whether a request may be in flight for *session*; ``None`` when unknown.

    ``True`` is the cheap direction: anything resident at all — a generation, a
    model merely held warm by ``keep_alive``, another slot's worker — leaves
    the session untouched, because a warm model is not proof of work but an
    absent one is proof of none.
    """
    base_url = local_endpoint(session, config)
    if not base_url:
        return None
    try:
        models = await asyncio.to_thread(running_models, base_url, timeout=DEFAULT_TIMEOUT_SECONDS)
    except Exception:
        logger.debug(
            "provider liveness probe failed for %s", getattr(session, "id", session), exc_info=True
        )
        return None
    if models is None:
        return None
    return bool(models)
