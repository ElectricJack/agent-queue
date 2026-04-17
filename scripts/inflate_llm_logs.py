#!/usr/bin/env python3
"""Inflate JSONL LLM logs into a browsable folder structure.

Usage:
    python scripts/inflate_llm_logs.py [DATE]
    python scripts/inflate_llm_logs.py              # today
    python scripts/inflate_llm_logs.py 2026-04-11   # specific date
    python scripts/inflate_llm_logs.py --all         # all dates

Output structure:
    ~/.agent-queue/logs/llm/2026-04-11/inflated/
      agent-queue/
        001_tell-me-about-this-project/
          001_turn.md
          002_turn.md
      moss-and-spade/
        001_sync-workspaces/
          001_turn.md
      _system/
        001_reflection-task-completed/
          001_turn.md
"""

import json
import os
import re
import sys
import textwrap
from datetime import date
from pathlib import Path

DATA_DIR = os.path.expanduser("~/.agent-queue/logs/llm")

# Regex to extract project ID from context prefix or ACTIVE PROJECT line
_PROJECT_RE = re.compile(
    r"(?:"
    r"ACTIVE PROJECT:\s*`([^`]+)`"  # system prompt: ACTIVE PROJECT: `foo`
    r"|channel for project\s*`([^`]+)`"  # message prefix: channel for project `foo`
    r"|NOTES MODE for project\s*'([^']+)'"  # notes: NOTES MODE for project 'foo'
    r"|project_id='([^']+)'"  # context hint: project_id='foo'
    r")"
)


def extract_project_id(entries: list[dict]) -> str:
    """Extract the project ID from a conversation's entries.

    Searches the system prompt and first user message for project context.
    Returns the project ID or '_system' if none found.
    """
    for entry in entries[:2]:  # check first 2 entries at most
        # Check system prompt
        system = entry.get("input", {}).get("system", "")
        if system:
            m = _PROJECT_RE.search(system)
            if m:
                return next(g for g in m.groups() if g)

        # Check messages
        for msg in entry.get("input", {}).get("messages", []):
            content = msg.get("content", "")
            if isinstance(content, str):
                m = _PROJECT_RE.search(content)
                if m:
                    return next(g for g in m.groups() if g)

    return "_system"


def slugify(text: str, max_len: int = 50) -> str:
    """Convert text to a filesystem-safe slug."""
    text = text.lower()
    # Strip common prefixes
    text = re.sub(r"^\[from \w+\]:\s*", "", text)
    text = re.sub(r"^\[context:.*?\]\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s-]+", "-", text).strip("-")
    return text[:max_len] or "unknown"


def extract_user_message(messages: list[dict]) -> str:
    """Get the last user message content (the new turn)."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            return content
    return ""


def extract_initial_request(messages: list[dict]) -> str:
    """Get the first user message (the conversation starter)."""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            return content
    return ""


def format_tool_uses(tool_uses: list[dict]) -> str:
    """Format tool calls into readable text."""
    if not tool_uses:
        return ""
    parts = []
    for tu in tool_uses:
        name = tu.get("name", "?")
        inp = tu.get("input", {})
        # Pretty-print the input but keep it compact
        try:
            inp_str = json.dumps(inp, indent=2)
        except (TypeError, ValueError):
            inp_str = str(inp)
        parts.append(f"### Tool Call: `{name}`\n\n```json\n{inp_str}\n```")
    return "\n\n".join(parts)


def format_new_messages(messages: list[dict], prev_message_count: int) -> str:
    """Format only the messages that are new since the previous turn.

    Between turns, the new messages are typically:
    - The assistant's prior response (tool calls)
    - Tool result(s) returned by the system
    """
    new_msgs = messages[prev_message_count:]
    if not new_msgs:
        return ""

    parts = []
    for msg in new_msgs:
        role = msg.get("role", "?")
        content = msg.get("content", "")

        if role == "assistant":
            # Assistant's prior tool calls (already shown in previous turn's output)
            # Show a brief note for continuity
            if isinstance(content, str) and "ToolUseBlock" in content:
                parts.append(f"*> Assistant called tools (see previous turn)*")
            elif isinstance(content, list):
                parts.append(f"*> Assistant called tools (see previous turn)*")
            elif content:
                parts.append(f"**Assistant:**\n{content}")

        elif role == "user":
            # Could be a real user message or tool results
            if isinstance(content, list):
                # Tool results array
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_result":
                        tool_id = item.get("tool_use_id", "?")
                        result_content = item.get("content", "")
                        # Try to pretty-print JSON results
                        display = _format_tool_result(result_content)
                        parts.append(f"### Tool Result (`{tool_id}`)\n\n{display}")
                    elif isinstance(item, dict):
                        parts.append(f"```json\n{json.dumps(item, indent=2)}\n```")
            elif isinstance(content, str):
                # Check if it looks like a tool result wrapper
                if content.startswith("[{") and "tool_result" in content:
                    try:
                        parsed = json.loads(content)
                        for item in parsed:
                            if isinstance(item, dict) and item.get("type") == "tool_result":
                                display = _format_tool_result(item.get("content", ""))
                                parts.append(f"### Tool Result\n\n{display}")
                    except (json.JSONDecodeError, TypeError):
                        parts.append(f"**Tool Results:**\n\n{content}")
                else:
                    parts.append(content)

    return "\n\n".join(parts)


def _format_tool_result(content: str) -> str:
    """Try to pretty-print a tool result, handling JSON and plain text."""
    if not content:
        return "*(empty)*"
    # Try JSON pretty-print
    try:
        parsed = json.loads(content)
        formatted = json.dumps(parsed, indent=2)
        return f"```json\n{formatted}\n```"
    except (json.JSONDecodeError, TypeError):
        return f"```\n{content}\n```"


def format_turn(entry: dict, turn_num: int, total_messages: int,
                prev_message_count: int) -> str:
    """Format a single LLM call into a readable markdown file."""
    lines = []

    timestamp = entry.get("timestamp", "?")
    model = entry.get("model", "?")
    caller = entry.get("caller", "?")
    duration = entry.get("duration_ms", 0)
    error = entry.get("error")

    lines.append(f"# Turn {turn_num}")
    lines.append(f"**Time:** {timestamp}  ")
    lines.append(f"**Model:** {model}  ")
    lines.append(f"**Caller:** {caller}  ")
    lines.append(f"**Duration:** {duration}ms  ")
    lines.append(f"**Messages in context:** {total_messages}")
    lines.append("")

    # --- Input ---
    inp = entry.get("input", {})
    messages = inp.get("messages", [])

    if turn_num == 1:
        # First turn: show full system prompt, tools, and user message
        # — exactly as the LLM sees them.
        system = inp.get("system", "")
        if system:
            lines.append(f"## System Prompt\n\n{system}")
            lines.append("")

        # Full tool definitions if available, otherwise just names
        tools = inp.get("tools", [])
        if tools:
            lines.append(f"## Tools ({len(tools)})\n")
            lines.append(f"```json\n{json.dumps(tools, indent=2)}\n```")
            lines.append("")
        else:
            tool_names = inp.get("tool_names", [])
            if tool_names:
                lines.append(f"## Tools ({len(tool_names)}) — names only, schemas not logged\n")
                lines.append(", ".join(f"`{t}`" for t in tool_names))
                lines.append("")

        # Show the initial user message
        initial = extract_initial_request(messages)
        if initial:
            lines.append("## User Request\n")
            lines.append(initial)
            lines.append("")
    else:
        # Subsequent turns: show what's new since last turn
        new_context = format_new_messages(messages, prev_message_count)
        if new_context:
            lines.append("## Context Since Last Turn\n")
            lines.append(new_context)
            lines.append("")

    # --- Output: what the LLM responded with ---
    out = entry.get("output", {})
    text_parts = out.get("text_parts", [])
    tool_uses = out.get("tool_uses", [])

    lines.append("## LLM Response\n")

    if text_parts:
        for part in text_parts:
            lines.append(part)
        lines.append("")

    if tool_uses:
        lines.append(format_tool_uses(tool_uses))
        lines.append("")

    if not text_parts and not tool_uses:
        lines.append("*(empty response)*\n")

    if error:
        lines.append(f"## Error\n\n```\n{error}\n```\n")

    # Token estimates
    input_tokens = inp.get("input_tokens_est", 0)
    output_tokens = out.get("output_tokens_est", 0)
    if input_tokens or output_tokens:
        lines.append(f"---\n*Tokens: ~{input_tokens} in, ~{output_tokens} out*")

    return "\n".join(lines)


def detect_conversations(entries: list[dict]) -> list[list[dict]]:
    """Group sequential log entries into conversations.

    A new conversation starts when:
    - The messages array has only 1 entry (fresh start)
    - The initial user message changes from the previous group
    - There's a gap of > 5 minutes between entries
    """
    if not entries:
        return []

    conversations: list[list[dict]] = []
    current: list[dict] = []
    prev_initial = None
    prev_timestamp = None

    for entry in entries:
        messages = entry.get("input", {}).get("messages", [])
        msg_count = len(messages)
        initial = extract_initial_request(messages)
        timestamp = entry.get("timestamp", "")

        # Detect time gap (> 5 min)
        time_gap = False
        if prev_timestamp and timestamp:
            try:
                from datetime import datetime, timezone

                # Parse ISO timestamps
                cur_t = datetime.fromisoformat(timestamp)
                prev_t = datetime.fromisoformat(prev_timestamp)
                if (cur_t - prev_t).total_seconds() > 300:
                    time_gap = True
            except (ValueError, TypeError):
                pass

        # New conversation?
        new_convo = False
        if not current:
            new_convo = True
        elif msg_count <= 1:
            new_convo = True
        elif initial != prev_initial:
            new_convo = True
        elif time_gap:
            new_convo = True

        if new_convo and current:
            conversations.append(current)
            current = []

        current.append(entry)
        prev_initial = initial
        prev_timestamp = timestamp

    if current:
        conversations.append(current)

    return conversations


def inflate_date(date_dir: Path) -> None:
    """Inflate a single date's logs."""
    jsonl_path = date_dir / "chat_provider.jsonl"
    if not jsonl_path.exists():
        print(f"  No chat_provider.jsonl in {date_dir}")
        return

    # Read all entries
    entries = []
    with open(jsonl_path) as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"  Warning: bad JSON on line {line_num}: {e}")

    if not entries:
        print(f"  No entries in {jsonl_path}")
        return

    # Group into conversations
    conversations = detect_conversations(entries)

    # Create output directory
    out_dir = date_dir / "inflated"
    # Clean previous output
    if out_dir.exists():
        import shutil

        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Group conversations by project
    project_convs: dict[str, list[list[dict]]] = {}
    for conv_entries in conversations:
        project = extract_project_id(conv_entries)
        project_convs.setdefault(project, []).append(conv_entries)

    total_convs = 0
    for project in sorted(project_convs):
        convs = project_convs[project]
        project_dir = out_dir / project
        project_dir.mkdir(parents=True, exist_ok=True)

        for conv_idx, conv_entries in enumerate(convs, 1):
            first_messages = conv_entries[0].get("input", {}).get("messages", [])
            initial_msg = extract_initial_request(first_messages)
            slug = slugify(initial_msg)
            folder_name = f"{conv_idx:03d}_{slug}"
            conv_dir = project_dir / folder_name
            conv_dir.mkdir(parents=True, exist_ok=True)

            prev_msg_count = 0
            for turn_idx, entry in enumerate(conv_entries, 1):
                msg_count = len(entry.get("input", {}).get("messages", []))
                content = format_turn(entry, turn_idx, msg_count, prev_msg_count)
                file_path = conv_dir / f"{turn_idx:03d}_turn.md"
                file_path.write_text(content, encoding="utf-8")
                prev_msg_count = msg_count

            print(f"  {project}/{folder_name}/ ({len(conv_entries)} turns)")
            total_convs += 1

    print(
        f"  -> {out_dir} ({total_convs} conversations across "
        f"{len(project_convs)} projects, {len(entries)} total turns)"
    )


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else None

    base = Path(DATA_DIR)
    if not base.exists():
        print(f"Log directory not found: {base}")
        sys.exit(1)

    if arg == "--all":
        date_dirs = sorted(d for d in base.iterdir() if d.is_dir() and d.name != "inflated")
    elif arg:
        date_dirs = [base / arg]
    else:
        date_dirs = [base / date.today().isoformat()]

    for d in date_dirs:
        if not d.exists():
            print(f"No logs for {d.name}")
            continue
        print(f"Inflating {d.name}...")
        inflate_date(d)


if __name__ == "__main__":
    main()
