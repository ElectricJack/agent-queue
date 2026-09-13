"""Resolve a profile's ``extends`` against its template.  Pure — no I/O.

A generic worker is the same agent at a different reasoning level: same role,
same rules, same capabilities, same harness, different ``default_class``.
Storing that as one markdown file per (class x harness) rung meant the prompt
was copied once per rung and drifted the moment anyone edited one of them.

``extends`` makes a rung a *stub*.  It carries only what is genuinely its own
— its class and its operator-owned pool state — and inherits everything else
from the ``worker-<harness>`` template at load time::

    ---
    id: deep-high-claude
    extends: worker-claude
    ---

    ## Config
    ```json
    {"default_class": "deep-high", "lifecycle": "task"}
    ```

Because resolution happens on the way into the database, there is nothing to
regenerate when a template changes: every rung picks the edit up on the next
sync.  The only reason a rung file is ever rewritten is that its own state
changed (``aq pool scale``).

**One level.**  A template may not itself extend another profile.  That is a
deliberate limit rather than an unimplemented feature: two levels would make
"where does this value come from" a search instead of a lookup, and there is
no case for it here.  It also means cycles are impossible by construction.

Merge rule: the stub wins wherever it speaks.  Prompt sections and the three
capability namespaces are taken whole from whichever side authored them (a
stub that writes a ``## Role`` replaces the template's, it does not append),
while ``## Config`` merges key by key so a stub can override ``lifecycle``
without restating ``harness``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from src.profiles.parser import ParsedProfile

#: Returned by :func:`resolve_inheritance` when the template cannot be found.
#: Kept as a stable prefix so ``aq doctor`` and the sync error path can
#: recognise it without matching on the whole sentence.
MISSING_TEMPLATE_ERROR = "extends: no profile"


def resolve_inheritance(
    parsed: ParsedProfile,
    load_template: Callable[[str], ParsedProfile | None],
) -> tuple[ParsedProfile, list[str]]:
    """Return *parsed* with its template merged in, plus any errors.

    ``load_template`` maps a template id to its parsed profile, or ``None``
    when there is no such profile.  A stub whose template is missing is an
    *error*, not a warning: syncing it would install a worker with no role,
    no rules and no capabilities, which is worse than leaving the previous
    database row in place.

    A profile with no ``extends`` is returned unchanged, so this is safe to
    call on every profile on the sync path.
    """
    template_id = (parsed.frontmatter.extends or "").strip()
    if not template_id:
        return parsed, []
    if template_id == parsed.frontmatter.id:
        return parsed, [f"extends: '{template_id}' cannot extend itself"]

    template = load_template(template_id)
    if template is None:
        return parsed, [f"{MISSING_TEMPLATE_ERROR} '{template_id}' to extend"]
    if template.frontmatter.extends:
        return parsed, [
            f"extends: template '{template_id}' itself extends "
            f"'{template.frontmatter.extends}'; inheritance is one level only"
        ]
    if not template.is_valid:
        return parsed, [f"extends: template '{template_id}' does not parse"]

    # Config merges key by key — a stub states its class and its pool state
    # and inherits the harness, workspaces and permissions it shares with
    # every other rung.
    config = {**template.config, **parsed.config}

    # Everything else is whole-value: the stub either authored the section or
    # it did not.  ``capabilities`` is deliberately not merged namespace by
    # namespace; a partial grant is how a copied profile silently loses a
    # command it needs.
    def inherited(name: str):
        own = getattr(parsed, name)
        return own if own else getattr(template, name)

    merged = replace(
        parsed,
        config=config,
        tools=parsed.tools or template.tools,
        capabilities=(
            parsed.capabilities if parsed.capabilities is not None else template.capabilities
        ),
        mcp_servers=inherited("mcp_servers"),
        role=inherited("role"),
        rules=inherited("rules"),
        reflection=inherited("reflection"),
        install=inherited("install"),
    )
    return merged, []


__all__ = ["MISSING_TEMPLATE_ERROR", "resolve_inheritance"]
