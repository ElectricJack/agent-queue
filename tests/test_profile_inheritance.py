"""``extends``: a rung stub resolving against its worker template.

The point of inheritance here is that a derived worker holds *only* its own
state.  These cover what that costs at the seams: what a stub inherits, what
it may override, and what happens when its template is missing or itself
extends something.
"""

from __future__ import annotations

import pytest

from src.profiles.inheritance import MISSING_TEMPLATE_ERROR, resolve_inheritance
from src.profiles.parser import parse_profile

TEMPLATE = """---
id: worker-claude
name: "Claude · Worker (template)"
template: true
---

# Claude · Worker (template)

## Role
You are a generic coding worker.

## Rules
- Read before writing.

## Config
```json
{
  "harness": "claude",
  "lifecycle": "task",
  "needs_workspace": true,
  "default_class": "standard-high",
  "workspaces": ["project-repo"]
}
```

## Capabilities
```json
{"harness_tools": ["Bash", "Read"], "aq_commands": ["task_close"], "plugin_tools": []}
```

## MCP Servers
```json
[]
```
"""

STUB = """---
id: deep-high-claude
name: "Claude · Deep · High"
extends: worker-claude
---

# Claude · Deep · High

## Config
```json
{"default_class": "deep-high", "lifecycle": "pool", "max_active": 2}
```
"""


def _loader(**templates):
    def load(template_id):
        text = templates.get(template_id)
        return parse_profile(text) if text is not None else None

    return load


def _resolve(stub_text=STUB, **templates):
    return resolve_inheritance(parse_profile(stub_text), _loader(**{"worker-claude": TEMPLATE, **templates}))


def test_a_stub_inherits_the_prompt_and_capabilities_it_does_not_author():
    merged, errors = _resolve()

    assert errors == []
    assert merged.role.strip() == "You are a generic coding worker."
    assert "Read before writing" in merged.rules
    assert merged.capabilities == {
        "harness_tools": ["Bash", "Read"], "aq_commands": ["task_close"], "plugin_tools": []
    }


def test_config_merges_key_by_key_so_a_stub_states_only_what_differs():
    merged, _ = _resolve()

    # Its own: class and pool state.  Inherited: harness, workspace, and the
    # rest of the shared execution shape.
    assert merged.config["default_class"] == "deep-high"
    assert merged.config["lifecycle"] == "pool"
    assert merged.config["max_active"] == 2
    assert merged.config["harness"] == "claude"
    assert merged.config["needs_workspace"] is True
    assert merged.config["workspaces"] == ["project-repo"]


def test_a_stub_that_authors_a_section_replaces_it_rather_than_appending():
    own = STUB.replace(
        "# Claude · Deep · High\n",
        "# Claude · Deep · High\n\n## Role\nYou are a deep worker.\n",
    )

    merged, errors = _resolve(own)

    assert errors == []
    assert merged.role.strip() == "You are a deep worker."
    # Everything it did not author still comes from the template.
    assert "Read before writing" in merged.rules


def test_a_profile_without_extends_is_returned_untouched():
    parsed = parse_profile(TEMPLATE)

    merged, errors = resolve_inheritance(parsed, _loader())

    assert errors == []
    assert merged is parsed


def test_a_missing_template_is_an_error_not_a_half_profile():
    """Syncing a role-less, capability-less worker is worse than not syncing."""
    merged, errors = resolve_inheritance(parse_profile(STUB), _loader())

    assert merged.role == ""
    assert errors and MISSING_TEMPLATE_ERROR in errors[0]
    assert "worker-claude" in errors[0]


def test_inheritance_is_one_level_only():
    chained = TEMPLATE.replace("template: true", "template: true\nextends: worker-base")

    _, errors = _resolve(**{"worker-claude": chained})

    assert errors and "one level only" in errors[0]


def test_a_profile_cannot_extend_itself():
    self_extending = STUB.replace("extends: worker-claude", "extends: deep-high-claude")

    _, errors = _resolve(self_extending)

    assert errors and "cannot extend itself" in errors[0]


@pytest.mark.parametrize("broken", ['{"default_class": ', "not json at all"])
def test_a_template_that_does_not_parse_is_reported_rather_than_merged(broken):
    bad = TEMPLATE.replace(
        '{\n  "harness": "claude",\n  "lifecycle": "task",\n  "needs_workspace": true,\n'
        '  "default_class": "standard-high",\n  "workspaces": ["project-repo"]\n}',
        broken,
    )

    _, errors = _resolve(**{"worker-claude": bad})

    assert errors and "does not parse" in errors[0]
