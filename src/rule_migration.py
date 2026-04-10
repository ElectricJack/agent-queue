"""Phase 2 migration: convert active rule files to playbook markdown.

Implements the rule-to-playbook conversion described in ``playbooks.md`` §13
Phase 2.  User-created active rules are converted to single-node playbooks
with equivalent triggers and logic.  Passive rules are migrated to memory
files (contextual guidance, not workflows).

The conversion is **deterministic** (no LLM needed) — we generate valid
playbook markdown from the rule's trigger section and logic body.  The
resulting ``.md`` file is then compiled by the normal playbook pipeline
(LLM compilation into a JSON graph).

Migration workflow:
1. Scan vault rule directories for user-created active rules
2. For each rule, generate equivalent playbook markdown
3. Write the playbook to the correct vault location
4. Remove or archive the original rule file
5. Clean up derived hooks from the database

The operation is **idempotent**: re-running the migration skips rules
that already have a corresponding playbook file.

See ``docs/specs/design/playbooks.md`` §13 Phase 2 for specification.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime, timezone
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Default rules that are handled separately (they have hand-crafted playbook
# replacements in src/prompts/default_playbooks/).  These are NOT migrated by
# the generic rule-to-playbook converter.
_DEFAULT_RULE_IDS: frozenset[str] = frozenset(
    {
        "rule-post-action-reflection",
        "rule-spec-drift-detector",
        "rule-error-recovery-monitor",
        "rule-periodic-project-review",
        "rule-proactive-codebase-inspector",
        "rule-dependency-update-check",
    }
)


# ---------------------------------------------------------------------------
# Trigger conversion: rule trigger config → playbook trigger strings
# ---------------------------------------------------------------------------


def _seconds_to_timer_trigger(seconds: int) -> str:
    """Convert an interval in seconds to a ``timer.{N}{unit}`` string.

    Chooses the most human-readable unit:
    - Hours if the interval is a whole number of hours
    - Minutes otherwise

    Examples::

        >>> _seconds_to_timer_trigger(1800)
        'timer.30m'
        >>> _seconds_to_timer_trigger(3600)
        'timer.1h'
        >>> _seconds_to_timer_trigger(14400)
        'timer.4h'
        >>> _seconds_to_timer_trigger(86400)
        'timer.24h'
        >>> _seconds_to_timer_trigger(90)
        'timer.2m'
    """
    if seconds <= 0:
        return "timer.1m"

    if seconds >= 3600 and seconds % 3600 == 0:
        hours = seconds // 3600
        return f"timer.{hours}h"

    # Convert to minutes, rounding up to at least 1
    minutes = max(1, seconds // 60)
    return f"timer.{minutes}m"


def rule_trigger_to_playbook_triggers(trigger_config: dict | None) -> list[str]:
    """Convert a parsed rule trigger config to playbook trigger strings.

    Args:
        trigger_config: The trigger config dict as returned by
            ``RuleManager._parse_trigger()``.  Format:
            ``{"type": "periodic"|"event"|"cron", ...}``.

    Returns:
        List of playbook trigger strings (e.g. ``["task.completed"]``
        or ``["timer.30m"]``).  Returns an empty list if the trigger
        cannot be converted.

    Examples::

        >>> rule_trigger_to_playbook_triggers({"type": "periodic", "interval_seconds": 1800})
        ['timer.30m']
        >>> rule_trigger_to_playbook_triggers({"type": "event", "event_type": "task.completed"})
        ['task.completed']
        >>> rule_trigger_to_playbook_triggers({"type": "cron", "cron": "0 0 * * *"})
        ['cron.0 0 * * *']
    """
    if not trigger_config:
        return []

    trigger_type = trigger_config.get("type", "")

    if trigger_type == "periodic":
        seconds = trigger_config.get("interval_seconds", 0)
        if seconds > 0:
            return [_seconds_to_timer_trigger(seconds)]
        return []

    if trigger_type == "event":
        event_type = trigger_config.get("event_type", "")
        if event_type:
            return [event_type]
        return []

    if trigger_type == "cron":
        cron_expr = trigger_config.get("cron", trigger_config.get("expression", ""))
        if cron_expr:
            # Cron triggers use the event format: cron.{expression}
            return [f"cron.{cron_expr}"]
        return []

    return []


# ---------------------------------------------------------------------------
# Rule → Playbook markdown generation
# ---------------------------------------------------------------------------


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Split YAML frontmatter from markdown body."""
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}, content
    return meta, parts[2].strip()


def _extract_trigger_section(body: str) -> str:
    """Extract the ## Trigger section content from rule body."""
    in_trigger = False
    lines: list[str] = []
    for line in body.split("\n"):
        stripped = line.strip()
        if re.match(r"^##\s+Trigger", stripped, re.IGNORECASE):
            in_trigger = True
            continue
        if in_trigger:
            if stripped.startswith("## "):
                break
            lines.append(line)
    return "\n".join(lines).strip()


def _extract_logic_section(body: str) -> str:
    """Extract the ## Logic section content from rule body."""
    in_logic = False
    lines: list[str] = []
    for line in body.split("\n"):
        stripped = line.strip()
        if re.match(r"^##\s+Logic", stripped, re.IGNORECASE):
            in_logic = True
            continue
        if in_logic:
            if stripped.startswith("## "):
                break
            lines.append(line)
    return "\n".join(lines).strip()


def _extract_intent_section(body: str) -> str:
    """Extract the ## Intent section content from rule body."""
    in_intent = False
    lines: list[str] = []
    for line in body.split("\n"):
        stripped = line.strip()
        if re.match(r"^##\s+Intent", stripped, re.IGNORECASE):
            in_intent = True
            continue
        if in_intent:
            if stripped.startswith("## "):
                break
            lines.append(line)
    return "\n".join(lines).strip()


def _extract_title(body: str) -> str:
    """Extract the first H1 heading from markdown."""
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _parse_trigger_from_body(body: str) -> dict | None:
    """Parse trigger config from rule body content.

    Reuses the same patterns as ``RuleManager._parse_trigger()`` to ensure
    consistent behavior.
    """
    # Find the Trigger section
    trigger_section = _extract_trigger_section(body)

    # Fallback: inline **Trigger:** markers
    if not trigger_section:
        for line in body.split("\n"):
            m = re.match(r"\*\*Trigger:?\*\*:?\s*(.*)", line.strip())
            if m:
                trigger_section = m.group(1)
                break

    if not trigger_section:
        return None

    text = trigger_section.lower().strip()

    # Parse "every N minutes/hours/days"
    match = re.search(r"every\s+(\d+)\s*(minute|min|hour|hr|second|sec|day)s?", text)
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        if unit.startswith("day"):
            seconds = value * 86400
        elif unit.startswith("hour") or unit.startswith("hr"):
            seconds = value * 3600
        elif unit.startswith("min"):
            seconds = value * 60
        else:
            seconds = value
        return {"type": "periodic", "interval_seconds": seconds}

    # Parse "every hour/minute/day" (no number)
    match = re.search(r"every\s+(hour|hr|minute|min|second|sec|day)s?", text)
    if match:
        unit = match.group(1)
        if unit.startswith("day"):
            seconds = 86400
        elif unit.startswith("hour") or unit.startswith("hr"):
            seconds = 3600
        elif unit.startswith("min"):
            seconds = 60
        else:
            seconds = 1
        return {"type": "periodic", "interval_seconds": seconds}

    # Parse shorthand
    if re.search(r"\bweekly\b", text):
        return {"type": "periodic", "interval_seconds": 604800}
    if re.search(r"\bdaily\b", text):
        return {"type": "periodic", "interval_seconds": 86400}
    if re.search(r"\bhourly\b", text):
        return {"type": "periodic", "interval_seconds": 3600}

    # Check N hours/minutes/seconds (without "every")
    match = re.search(r"(?:check\s+)?(\d+)\s*(minute|min|hour|hr|second|sec|day)s?", text)
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        if unit.startswith("day"):
            seconds = value * 86400
        elif unit.startswith("hour") or unit.startswith("hr"):
            seconds = value * 3600
        elif unit.startswith("min"):
            seconds = value * 60
        else:
            seconds = value
        return {"type": "periodic", "interval_seconds": seconds}

    # Event-based triggers
    if re.search(r"when\s+(?:a\s+)?task\s+(?:is\s+)?completed", text):
        return {"type": "event", "event_type": "task.completed"}

    if re.search(r"when\s+(?:a\s+)?task\s+(?:is\s+)?(?:failed|fails)", text):
        return {"type": "event", "event_type": "task.failed"}

    if re.search(r"when\s+(?:an?\s+)?(?:agent\s+)?(?:crash|error)", text):
        return {"type": "event", "event_type": "error.agent_crash"}

    # Explicit event type references
    event_match = re.search(r"`?([\w]+\.[\w.]+)`?", text)
    if event_match:
        return {"type": "event", "event_type": event_match.group(1)}

    # Cron expressions
    cron_match = re.search(r"cron[:\s]+`?([*\d/,\-\s]{9,})`?", text)
    if cron_match:
        return {"type": "cron", "cron": cron_match.group(1).strip()}

    return None


def _compute_cooldown(trigger_config: dict | None) -> int | None:
    """Derive a reasonable cooldown from the trigger config.

    For periodic triggers, cooldown is half the interval (matching rule
    behavior).  For event triggers, no explicit cooldown is set (the
    playbook runtime handles its own cooldown).

    Returns:
        Cooldown in seconds, or ``None`` if no cooldown should be set.
    """
    if not trigger_config:
        return None
    if trigger_config.get("type") == "periodic":
        interval = trigger_config.get("interval_seconds", 0)
        if interval > 0:
            # Match rule behavior: cooldown = interval / 2
            return max(30, interval // 2)
    return None


def _rule_id_to_playbook_id(rule_id: str) -> str:
    """Convert a rule ID to a playbook-style ID.

    Strips the ``rule-`` prefix if present, since playbook IDs don't
    use a type prefix.

    Examples::

        >>> _rule_id_to_playbook_id("rule-check-test-coverage")
        'check-test-coverage'
        >>> _rule_id_to_playbook_id("my-custom-rule")
        'my-custom-rule'
    """
    if rule_id.startswith("rule-"):
        return rule_id[5:]
    return rule_id


def _build_playbook_body(title: str, intent: str, logic: str, full_body: str) -> str:
    """Build the natural language body of a playbook from rule sections.

    Combines the rule's title, intent, and logic into a cohesive playbook
    description suitable for LLM compilation into a single-node graph.

    If no structured sections are found, falls back to using the full body
    after stripping trigger-related content.
    """
    parts: list[str] = []

    # Title
    if title:
        parts.append(f"# {title}")
        parts.append("")

    # Intent as the opening description
    if intent:
        parts.append(intent)
        parts.append("")

    # Logic as the detailed instructions
    if logic:
        parts.append(logic)
    elif not intent:
        # No structured sections — use the full body minus trigger section
        # and title
        cleaned = _strip_trigger_and_title(full_body)
        if cleaned.strip():
            parts.append(cleaned.strip())

    return "\n".join(parts)


def _strip_trigger_and_title(body: str) -> str:
    """Remove the ## Trigger section and # Title from the body.

    Returns the remaining content, which should be suitable as a
    playbook body.
    """
    lines = body.split("\n")
    result: list[str] = []
    skip_section = False
    skipped_title = False
    saw_blank_in_trigger = False

    for line in lines:
        stripped = line.strip()

        # Skip the title line
        if not skipped_title and stripped.startswith("# ") and not stripped.startswith("## "):
            skipped_title = True
            continue

        # Skip the Trigger section
        if re.match(r"^##\s+Trigger", stripped, re.IGNORECASE):
            skip_section = True
            saw_blank_in_trigger = False
            continue
        if skip_section:
            if stripped.startswith("## "):
                # Next section heading — stop skipping
                skip_section = False
                result.append(line)
            elif not stripped:
                # Blank line within trigger section
                saw_blank_in_trigger = True
            elif saw_blank_in_trigger and stripped:
                # Non-empty, non-heading line after a blank line in the trigger
                # section — this is likely content after the trigger, not part
                # of it.  Stop skipping.
                skip_section = False
                result.append(line)
            # else: non-empty line immediately after ## Trigger — skip (trigger content)
            continue

        result.append(line)

    return "\n".join(result)


def generate_playbook_markdown(
    rule_content: str,
    *,
    rule_id: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Generate playbook markdown from a rule file's content.

    This is the core conversion function.  Given a rule's raw file content
    (with YAML frontmatter), it produces equivalent playbook markdown.

    Args:
        rule_content: The raw content of the rule ``.md`` file (with
            YAML frontmatter).
        rule_id: Override for the rule ID (used when the frontmatter
            lacks an ``id`` field).
        project_id: Override for the project scope (used when the
            frontmatter lacks a ``project_id`` field).

    Returns:
        Dict with:
        - ``success`` (bool): Whether conversion succeeded.
        - ``playbook_markdown`` (str): The generated playbook markdown
          (only when ``success=True``).
        - ``playbook_id`` (str): The generated playbook ID.
        - ``triggers`` (list[str]): The playbook trigger strings.
        - ``scope`` (str): The playbook scope.
        - ``error`` (str): Error message (only when ``success=False``).
        - ``rule_type`` (str): The original rule type.
        - ``is_passive`` (bool): Whether this is a passive rule.
    """
    meta, body = _parse_frontmatter(rule_content)

    # Extract rule metadata
    rid = rule_id or meta.get("id", "")
    rtype = meta.get("type", "passive")
    pid = project_id or meta.get("project_id")

    if not rid:
        return {
            "success": False,
            "error": "Rule has no ID (neither in frontmatter nor provided)",
            "rule_type": rtype,
            "is_passive": rtype != "active",
        }

    # Passive rules are not converted to playbooks
    if rtype != "active":
        return {
            "success": False,
            "playbook_id": _rule_id_to_playbook_id(rid),
            "error": "Passive rules are not converted to playbooks; "
            "they become memory files instead",
            "rule_type": rtype,
            "is_passive": True,
        }

    # Parse trigger
    trigger_config = _parse_trigger_from_body(body)
    triggers = rule_trigger_to_playbook_triggers(trigger_config)

    if not triggers:
        return {
            "success": False,
            "playbook_id": _rule_id_to_playbook_id(rid),
            "error": f"Could not parse trigger from rule '{rid}'",
            "rule_type": rtype,
            "is_passive": False,
        }

    # Determine scope
    if pid and pid != "None":
        scope = "project"
    else:
        scope = "system"

    # Generate playbook ID
    playbook_id = _rule_id_to_playbook_id(rid)

    # Extract sections
    title = _extract_title(body)
    intent = _extract_intent_section(body)
    logic = _extract_logic_section(body)

    # Build playbook body
    playbook_body = _build_playbook_body(title, intent, logic, body)

    # Compute cooldown
    cooldown = _compute_cooldown(trigger_config)

    # Build frontmatter
    fm: dict[str, Any] = {
        "id": playbook_id,
        "triggers": triggers,
        "scope": scope,
    }
    if cooldown is not None:
        fm["cooldown"] = cooldown

    # Serialize
    frontmatter_yaml = yaml.dump(fm, default_flow_style=False, sort_keys=False).strip()
    playbook_markdown = f"---\n{frontmatter_yaml}\n---\n\n{playbook_body}\n"

    return {
        "success": True,
        "playbook_markdown": playbook_markdown,
        "playbook_id": playbook_id,
        "triggers": triggers,
        "scope": scope,
        "rule_type": rtype,
        "is_passive": False,
    }


# ---------------------------------------------------------------------------
# Batch migration
# ---------------------------------------------------------------------------


def _get_all_rule_dirs(storage_root: str) -> list[tuple[str, str | None]]:
    """Return all rule directories as (abs_path, project_id|None).

    Scans vault playbook directories and legacy memory/*/rules/ locations.
    """
    result = []
    storage_root = os.path.expanduser(storage_root)

    # Vault system playbooks
    system_dir = os.path.join(storage_root, "vault", "system", "playbooks")
    if os.path.isdir(system_dir):
        result.append((system_dir, None))

    # Vault project playbooks
    projects_dir = os.path.join(storage_root, "vault", "projects")
    if os.path.isdir(projects_dir):
        for project_id in sorted(os.listdir(projects_dir)):
            playbooks_dir = os.path.join(projects_dir, project_id, "playbooks")
            if os.path.isdir(playbooks_dir):
                result.append((playbooks_dir, project_id))

    # Legacy memory/ locations
    memory_root = os.path.join(storage_root, "memory")
    if os.path.isdir(memory_root):
        for scope_dir in sorted(os.listdir(memory_root)):
            rules_dir = os.path.join(memory_root, scope_dir, "rules")
            if os.path.isdir(rules_dir):
                pid = None if scope_dir in ("global", "None") else scope_dir
                result.append((rules_dir, pid))

    return result


def _vault_playbook_path(storage_root: str, playbook_id: str, project_id: str | None) -> str:
    """Return the vault path where a playbook file should be written."""
    storage_root = os.path.expanduser(storage_root)
    if project_id and project_id != "None":
        return os.path.join(
            storage_root, "vault", "projects", project_id, "playbooks", f"{playbook_id}.md"
        )
    return os.path.join(storage_root, "vault", "system", "playbooks", f"{playbook_id}.md")


def _vault_memory_path(storage_root: str, filename: str, project_id: str | None) -> str:
    """Return the vault path where a passive rule should be written as memory."""
    storage_root = os.path.expanduser(storage_root)
    if project_id and project_id != "None":
        return os.path.join(
            storage_root,
            "vault",
            "projects",
            project_id,
            "memory",
            filename,
        )
    return os.path.join(storage_root, "vault", "system", "memory", filename)


def _is_default_rule(rule_id: str) -> bool:
    """Check whether a rule ID belongs to the set of default rules."""
    return rule_id in _DEFAULT_RULE_IDS


def _is_playbook_file(meta: dict) -> bool:
    """Check if a file is already in playbook format (has triggers in frontmatter)."""
    return bool(meta.get("triggers"))


def migrate_rules_to_playbooks(
    storage_root: str,
    *,
    dry_run: bool = False,
    include_defaults: bool = False,
) -> dict[str, Any]:
    """Migrate user-created active rules to playbook markdown files.

    Scans all rule directories (vault + legacy memory locations) for active
    rules and converts each to an equivalent playbook markdown file.

    Default rules (the six bundled rules) are skipped unless
    ``include_defaults=True`` — they have hand-crafted playbook replacements
    already installed by :func:`~src.vault.ensure_default_playbooks`.

    Passive rules are migrated to memory files in the vault (they are
    contextual guidance, not workflows).

    The operation is **idempotent**: rules that already have a corresponding
    playbook file at the destination are skipped.

    Args:
        storage_root: Base data directory (e.g. ``~/.agent-queue``).
        dry_run: If True, scan and report what would happen without
            writing any files.
        include_defaults: If True, also migrate default rules (normally
            these are handled by ensure_default_playbooks).

    Returns:
        Dict with migration statistics:
        - ``migrated``: Number of rules converted to playbooks.
        - ``passive_migrated``: Number of passive rules moved to memory.
        - ``skipped_existing``: Number of rules skipped (playbook exists).
        - ``skipped_default``: Number of default rules skipped.
        - ``skipped_already_playbook``: Number of files already in playbook format.
        - ``errors``: Number of conversion errors.
        - ``details``: List of per-rule detail strings.
        - ``dry_run``: Whether this was a dry run.
    """
    stats: dict[str, Any] = {
        "migrated": 0,
        "passive_migrated": 0,
        "skipped_existing": 0,
        "skipped_default": 0,
        "skipped_already_playbook": 0,
        "errors": 0,
        "details": [],
        "dry_run": dry_run,
    }

    rule_dirs = _get_all_rule_dirs(storage_root)

    for rules_dir, project_id in rule_dirs:
        if not os.path.isdir(rules_dir):
            continue

        for filename in sorted(os.listdir(rules_dir)):
            if not filename.endswith(".md"):
                continue

            filepath = os.path.join(rules_dir, filename)
            try:
                with open(filepath) as f:
                    raw = f.read()
            except Exception as e:
                stats["errors"] += 1
                stats["details"].append(f"ERROR read {filepath}: {e}")
                logger.warning("Failed to read rule file %s: %s", filepath, e)
                continue

            meta, body = _parse_frontmatter(raw)
            rule_id = meta.get("id", filename[:-3])
            rule_type = meta.get("type", "passive")
            rule_pid = meta.get("project_id") or project_id

            # Skip files that are already in playbook format
            if _is_playbook_file(meta):
                stats["skipped_already_playbook"] += 1
                stats["details"].append(f"SKIP {rule_id}: already in playbook format")
                continue

            # Skip default rules (unless include_defaults)
            if not include_defaults and _is_default_rule(rule_id):
                stats["skipped_default"] += 1
                stats["details"].append(
                    f"SKIP {rule_id}: default rule (handled by ensure_default_playbooks)"
                )
                continue

            # Handle passive rules → memory files
            if rule_type != "active":
                dest_path = _vault_memory_path(storage_root, f"{rule_id}.md", rule_pid)
                if os.path.exists(dest_path):
                    stats["skipped_existing"] += 1
                    stats["details"].append(f"SKIP passive {rule_id}: memory file already exists")
                    continue

                if dry_run:
                    stats["passive_migrated"] += 1
                    stats["details"].append(f"DRY-RUN passive {rule_id}: would move to {dest_path}")
                    continue

                try:
                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    # Write as a clean memory file (strip rule-specific frontmatter)
                    memory_content = _build_memory_file(meta, body)
                    with open(dest_path, "w") as f:
                        f.write(memory_content)
                    stats["passive_migrated"] += 1
                    stats["details"].append(f"MIGRATE passive {rule_id} → {dest_path}")
                    logger.info("Migrated passive rule %s to memory: %s", rule_id, dest_path)
                except Exception as e:
                    stats["errors"] += 1
                    stats["details"].append(f"ERROR passive {rule_id}: {e}")
                    logger.warning("Failed to migrate passive rule %s: %s", rule_id, e)
                continue

            # Active rule → playbook conversion
            result = generate_playbook_markdown(raw, rule_id=rule_id, project_id=rule_pid)

            if not result.get("success"):
                stats["errors"] += 1
                error = result.get("error", "unknown error")
                stats["details"].append(f"ERROR {rule_id}: {error}")
                logger.warning("Failed to convert rule %s: %s", rule_id, error)
                continue

            playbook_id = result["playbook_id"]
            dest_path = _vault_playbook_path(storage_root, playbook_id, rule_pid)

            # Idempotent: skip if destination already exists
            if os.path.exists(dest_path):
                stats["skipped_existing"] += 1
                stats["details"].append(f"SKIP {rule_id}: playbook {playbook_id}.md already exists")
                continue

            if dry_run:
                stats["migrated"] += 1
                stats["details"].append(
                    f"DRY-RUN {rule_id} → {playbook_id} "
                    f"(triggers={result['triggers']}, scope={result['scope']})"
                )
                continue

            # Write the playbook file
            try:
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                with open(dest_path, "w") as f:
                    f.write(result["playbook_markdown"])
                stats["migrated"] += 1
                stats["details"].append(
                    f"MIGRATE {rule_id} → {playbook_id}.md "
                    f"(triggers={result['triggers']}, scope={result['scope']})"
                )
                logger.info(
                    "Migrated rule %s → playbook %s (triggers=%s, scope=%s)",
                    rule_id,
                    playbook_id,
                    result["triggers"],
                    result["scope"],
                )
            except Exception as e:
                stats["errors"] += 1
                stats["details"].append(f"ERROR writing {playbook_id}.md: {e}")
                logger.warning("Failed to write playbook for rule %s: %s", rule_id, e)

    if not dry_run and (stats["migrated"] or stats["passive_migrated"]):
        logger.info(
            "Rule-to-playbook migration complete: %d active → playbooks, "
            "%d passive → memory, %d skipped, %d errors",
            stats["migrated"],
            stats["passive_migrated"],
            stats["skipped_existing"] + stats["skipped_default"],
            stats["errors"],
        )

    return stats


def _build_memory_file(meta: dict, body: str) -> str:
    """Build a clean memory file from a passive rule's content.

    Strips rule-specific frontmatter (type, hooks) and keeps only the
    content that serves as contextual guidance.
    """
    # Build minimal frontmatter for the memory file
    memory_meta: dict[str, Any] = {}
    if meta.get("id"):
        memory_meta["id"] = meta["id"]
    if meta.get("project_id"):
        memory_meta["project_id"] = meta["project_id"]
    if meta.get("created"):
        memory_meta["created"] = meta["created"]
    memory_meta["migrated_from"] = "rule"
    memory_meta["migrated_at"] = datetime.now(timezone.utc).isoformat()

    frontmatter = yaml.dump(memory_meta, default_flow_style=False, sort_keys=False).strip()
    return f"---\n{frontmatter}\n---\n\n{body}\n"


# ---------------------------------------------------------------------------
# Cleanup helpers (for post-migration hook removal)
# ---------------------------------------------------------------------------


async def cleanup_migrated_rule_hooks(
    db: Any,
    rule_ids: list[str],
) -> dict[str, Any]:
    """Delete hooks associated with migrated rules from the database.

    After rules are converted to playbooks, their derived hooks are no longer
    needed — the playbook runtime handles execution.  This function removes
    those hooks.

    Args:
        db: Database backend with ``delete_hooks_by_id_prefix`` and
            ``delete_hook`` methods.
        rule_ids: List of rule IDs whose hooks should be cleaned up.

    Returns:
        Dict with ``cleaned`` (count of hooks deleted) and ``errors``
        (count of failures).
    """
    stats = {"cleaned": 0, "errors": 0}

    for rule_id in rule_ids:
        prefix = f"rule-{rule_id}-"
        try:
            deleted = await db.delete_hooks_by_id_prefix(prefix)
            if deleted:
                stats["cleaned"] += deleted
                logger.info("Cleaned up %d hooks for migrated rule %s", deleted, rule_id)
        except Exception as e:
            stats["errors"] += 1
            logger.warning("Failed to clean up hooks for rule %s: %s", rule_id, e)

    return stats


async def remove_migrated_rule_files(
    storage_root: str,
    rule_ids: list[str],
) -> dict[str, Any]:
    """Archive or remove original rule files after successful migration.

    Moves rule files to an ``archived_rules/`` directory for safety,
    rather than deleting them outright.

    Args:
        storage_root: Base data directory (e.g. ``~/.agent-queue``).
        rule_ids: List of rule IDs whose files should be archived.

    Returns:
        Dict with ``archived`` and ``errors`` counts.
    """
    stats = {"archived": 0, "not_found": 0, "errors": 0}
    storage_root = os.path.expanduser(storage_root)
    archive_dir = os.path.join(storage_root, "vault", "archived_rules")

    for rule_id in rule_ids:
        # Search all rule locations
        found = _find_rule_file(storage_root, rule_id)
        if not found:
            stats["not_found"] += 1
            continue

        filepath, _ = found
        try:
            os.makedirs(archive_dir, exist_ok=True)
            dest = os.path.join(archive_dir, os.path.basename(filepath))
            shutil.move(filepath, dest)
            stats["archived"] += 1
            logger.info("Archived migrated rule file: %s → %s", filepath, dest)
        except Exception as e:
            stats["errors"] += 1
            logger.warning("Failed to archive rule file %s: %s", filepath, e)

    return stats


def _find_rule_file(storage_root: str, rule_id: str) -> tuple[str, str | None] | None:
    """Find a rule file by ID across all vault scopes."""
    # Check vault system playbooks
    system_dir = os.path.join(storage_root, "vault", "system", "playbooks")
    candidate = os.path.join(system_dir, f"{rule_id}.md")
    if os.path.isfile(candidate):
        # Only return if it's actually a rule file (has type in frontmatter)
        try:
            with open(candidate) as f:
                meta, _ = _parse_frontmatter(f.read())
            if meta.get("type") in ("active", "passive"):
                return candidate, None
        except Exception:
            pass

    # Check vault project playbooks
    projects_dir = os.path.join(storage_root, "vault", "projects")
    if os.path.isdir(projects_dir):
        for project_id in sorted(os.listdir(projects_dir)):
            playbooks_dir = os.path.join(projects_dir, project_id, "playbooks")
            candidate = os.path.join(playbooks_dir, f"{rule_id}.md")
            if os.path.isfile(candidate):
                try:
                    with open(candidate) as f:
                        meta, _ = _parse_frontmatter(f.read())
                    if meta.get("type") in ("active", "passive"):
                        return candidate, project_id
                except Exception:
                    pass

    # Legacy memory/ locations
    memory_root = os.path.join(storage_root, "memory")
    if os.path.isdir(memory_root):
        for scope_dir in sorted(os.listdir(memory_root)):
            rules_dir = os.path.join(memory_root, scope_dir, "rules")
            candidate = os.path.join(rules_dir, f"{rule_id}.md")
            if os.path.isfile(candidate):
                pid = None if scope_dir in ("global", "None") else scope_dir
                return candidate, pid

    return None
