# Discord UI Components & Human-in-the-Loop Interaction Research

<!-- aq:historical -->
> **Historical working note.** Dated scratch material, kept for the record and
> never revised. Not instructions. Start at [the documentation
> home](../docs/README.md); see [historical
> material](../docs/history/README.md).

**Date:** 2026-03-14
**Project:** agent-queue
**Purpose:** Research Discord's UI capabilities and design a comprehensive plan for human-in-the-loop interaction commands.

---

## Table of Contents

1. [Discord UI Components Overview](#1-discord-ui-components-overview)
2. [Current Implementation Audit](#2-current-implementation-audit)
3. [Proposed Human-in-the-Loop Commands](#3-proposed-human-in-the-loop-commands)
4. [Implementation Plan](#4-implementation-plan)
5. [Discord API Limitations & Constraints](#5-discord-api-limitations--constraints)
6. [Example Code Snippets](#6-example-code-snippets)

---

## 1. Discord UI Components Overview

### 1.1 Buttons (`discord.ui.Button`)

Buttons are the simplest interactive component. They appear in action rows and trigger callbacks when clicked.

**Styles available:**
| Style | Enum | Color | Use Case |
|-------|------|-------|----------|
| Primary | `discord.ButtonStyle.primary` | Blurple/Blue | Main actions (Approve, Submit) |
| Secondary | `discord.ButtonStyle.secondary` | Gray | Neutral actions (Cancel, Dismiss) |
| Success | `discord.ButtonStyle.success` | Green | Positive confirmations (Complete, Accept) |
| Danger | `discord.ButtonStyle.danger` | Red | Destructive actions (Delete, Reject) |
| Link | `discord.ButtonStyle.link` | Gray w/ icon | External URLs (no custom_id, opens URL) |

**Properties:**
- `custom_id`: Up to 100 characters. Used to identify button clicks in handlers.
- `label`: Up to 80 characters. Text displayed on button.
- `emoji`: Optional emoji icon (Unicode or custom Discord emoji).
- `disabled`: Boolean to gray out button.
- `url`: For link-style buttons only (opens external URL, no callback).
- `row`: 0–4, controls which action row the button appears in.

**Limits:** Max 5 buttons per action row, max 5 action rows per message = 25 buttons total.

### 1.2 Select Menus

Discord provides several select menu types, all sharing similar API patterns:

#### String Select (`discord.ui.Select`)
- User picks from predefined options (up to 25 options).
- Each option has `label`, `value`, `description` (optional), `emoji` (optional), `default` (bool).
- `min_values` / `max_values` control multi-select (1–25).
- `placeholder`: Grayed-out hint text when nothing selected.

#### User Select (`discord.ui.UserSelect`)
- Lets user pick Discord users from the server.
- Useful for assigning tasks to team members.

#### Role Select (`discord.ui.RoleSelect`)
- Pick Discord roles (e.g., for permission-based workflows).

#### Mentionable Select (`discord.ui.MentionableSelect`)
- Combined user + role picker.

#### Channel Select (`discord.ui.ChannelSelect`)
- Pick channels (with optional `channel_types` filter).

**Limits:** Each select menu takes a full action row. Max 5 action rows per message.

### 1.3 Modals / Dialogs (`discord.ui.Modal`)

Modals are pop-up forms that appear when triggered by a button or select interaction.

**Components within modals:**
- `discord.ui.TextInput` — text field or text area.
  - `style`: `TextStyle.short` (single-line) or `TextStyle.long` (paragraph/textarea).
  - `label`: Field label (up to 45 chars).
  - `placeholder`: Hint text.
  - `default`: Pre-filled value.
  - `required`: Boolean.
  - `min_length` / `max_length`: Validation (0–4000).
  - `row`: 0–4 for positioning.

**Limits:**
- Max 5 TextInput components per modal.
- Modals can ONLY be triggered by button/select interactions, NOT by slash commands directly (you must send an interaction response first, e.g., a button that opens the modal).
- Modal submission has a 15-minute timeout.
- Only TextInput is supported in modals — no buttons, selects, or other components.

### 1.4 Embeds (`discord.Embed`)

Rich content cards for structured data display (read-only, not interactive).

**Properties:**
- `title`: Up to 256 characters.
- `description`: Up to 4096 characters.
- `color`: Sidebar color (int or `discord.Color`).
- `url`: Clickable title link.
- `timestamp`: Datetime displayed in footer.
- `author`: Name, icon_url, url (name up to 256 chars).
- `footer`: Text and icon_url (text up to 2048 chars).
- `thumbnail`: Small image (top-right).
- `image`: Large image (bottom).
- `fields`: Up to 25 fields, each with name (256), value (1024), inline (bool).

**Limits:**
- Total embed content: 6000 characters across all text fields.
- Up to 10 embeds per message.
- Field values support basic markdown.

### 1.5 Action Rows (`discord.ActionRow` / implicit)

In discord.py, action rows are managed implicitly by `discord.ui.View`:
- Each View can have up to 5 action rows.
- Buttons share rows (up to 5 per row).
- Select menus take a full row each.
- `row=` parameter on components controls placement (0–4).

### 1.6 Ephemeral Messages

Messages visible only to the interaction user:
- Set via `ephemeral=True` on `interaction.response.send_message()`.
- Cannot be edited by others or referenced.
- Disappear on client restart.
- Useful for private confirmations, error messages, and sensitive data.

### 1.7 Autocomplete

Slash command options can have dynamic autocomplete:
- Decorated with `@command.autocomplete('param_name')`.
- Returns up to 25 `app_commands.Choice` suggestions.
- Fires as user types — must respond within 3 seconds.

---

## 2. Current Implementation Audit

### 2.1 Components Already in Use

The agent-queue project already has extensive Discord UI integration:

**Buttons (heavily used):**
- `TaskApprovalView`: Approve (✅ green) + Restart (🔄 secondary) buttons for PR approval
- `TaskStartedView`: Stop Task button (🛑 danger)
- `TaskFailedView`: Retry (🔄), Skip (⏭), View Error (📋) buttons
- `TaskBlockedView`: Multiple action buttons for blocked tasks
- `AgentQuestionView`: Reply button that opens modal
- `MenuView`: 10-button persistent dashboard (Status, Tasks, Projects, Agents, Notes, Add Task, Restart, Hooks, Toggle Orchestrator)
- `NotesView`: Paginated note selection (up to 20 note buttons)
- `NoteContentView`: Plan, Dismiss, Delete buttons

**Modals (extensively used):**
- `ProjectInfoModal`: 4-field project creation form
- `GitHubRepoModal` / `ExistingRepoModal`: Repo configuration
- `AgentReplyModal`: Reply to agent question
- `_AddTaskMenuModal`: Quick task creation
- `_FileEditModal`: Edit file content in-place
- Hook wizard modals: `_HookPromptModal`, `_HookCooldownModal`, `_HookLLMConfigModal`, etc.

**Select Menus:**
- File/directory browser navigation (string selects)
- Hook event category selection
- Hook periodic unit/value selection

**Embeds:**
- `EmbedStyle` enum: SUCCESS (green), ERROR (red), WARNING (amber), INFO (blue), CRITICAL (dark red)
- Factory functions: `make_embed()`, `success_embed()`, `error_embed()`, `warning_embed()`, `info_embed()`, `critical_embed()`, `status_embed()`, `tree_view_embed()`
- Auto-truncation to respect 6000-char limit

**Views:**
- Persistent views (`timeout=None`): MenuView (dashboard)
- Long-lived views (`timeout=86400`): TaskApprovalView (24h)
- Standard views (`timeout=3600`): Most interactive flows (1h)

### 2.2 Custom ID Conventions

Current convention uses colon-delimited format:
```
{domain}:{action}:{param}

Examples:
  notes:{project_id}:dismiss:{note_slug}
  notes:{project_id}:delete:{note_slug}
  notes:{project_id}:plan:{note_slug}
  notes:{project_id}:view:{slug}
  notes:{project_id}:page:{direction}
  hooks:edit:{hook_id}
  hooks:page:prev
  hooks:page:next
```

### 2.3 Architecture Pattern

Commands follow a **thin presentation layer** pattern:
1. Slash command receives interaction
2. Delegates to `CommandHandler.execute()` for business logic
3. Formats response as embed or plain text
4. Attaches View with interactive components if needed

No traditional cogs — all 85+ commands registered via `setup_commands(bot)`.

---

## 3. Proposed Human-in-the-Loop Commands

### 3.1 Task Approval & Review Enhancement

#### `/review-task <task_id>` — Rich Task Review Panel
**Purpose:** Present a comprehensive task review interface with inline actions.

**Components:**
- **Embed:** Task title, description, status, agent, branch, PR link, file changes summary, test results
- **Button Row 1:** ✅ Approve | 🔄 Request Changes | ❌ Reject | 📋 View Diff
- **Button Row 2:** 💬 Add Comment | 🏷️ Change Priority | 👤 Reassign

**Flow:**
1. User runs `/review-task task-123`
2. Bot sends rich embed with task details + action buttons
3. "Approve" → marks task complete, merges PR if applicable
4. "Request Changes" → opens modal for feedback text → reopens task with feedback
5. "Reject" → opens modal for rejection reason → blocks task
6. "View Diff" → sends ephemeral message with git diff (paginated if large)
7. "Add Comment" → opens modal → adds comment to task context
8. "Change Priority" → opens select menu (P0-Critical through P4-Low)
9. "Reassign" → user select menu to pick different agent

**Effort:** Medium (2–3 hours). Mostly composing existing functionality.

#### `/batch-approve` — Bulk Approval Interface
**Purpose:** Approve multiple pending tasks at once.

**Components:**
- **Embed:** List of all AWAITING_APPROVAL tasks with PR links
- **Select Menu:** Multi-select of tasks to approve (max 25)
- **Button Row:** ✅ Approve Selected | ❌ Reject Selected | 🔄 Refresh

**Flow:**
1. Shows all tasks awaiting approval across projects
2. User multi-selects tasks from dropdown
3. Clicks Approve/Reject → batch operation with progress indicator

**Effort:** Medium (2–3 hours).

### 3.2 Task Triage & Prioritization

#### `/triage` — Interactive Task Triage Board
**Purpose:** Review and prioritize new/unassigned tasks.

**Components:**
- **Embed:** Shows one task at a time with full details
- **Select Menu:** Priority selection (P0–P4)
- **Button Row 1:** ✅ Accept & Queue | ⏭ Skip | 🗑️ Delete
- **Button Row 2:** 📝 Edit | 🔗 Add Dependency | 👤 Assign Profile
- **Button Row 3:** ◀ Previous | Task 3/12 | Next ▶

**Flow:**
1. Loads all READY tasks sorted by creation date
2. User reviews each task, sets priority, accepts or skips
3. Navigation buttons cycle through the task list
4. Accept → task stays in queue. Skip → moves to next without action.

**Effort:** Medium-High (3–4 hours). Requires pagination state management.

#### `/prioritize <project_id>` — Drag-and-Drop Priority Editor
**Purpose:** Reorder task priorities within a project via select menus.

**Components:**
- **Embed:** Numbered list of tasks with current priorities
- **Select Menu 1:** Pick task to move
- **Select Menu 2:** Pick new position (1–N)
- **Button:** Apply Changes | Cancel

**Effort:** Medium (2–3 hours).

### 3.3 Error Resolution & Recovery

#### `/resolve-error <task_id>` — Interactive Error Resolution
**Purpose:** Present error details with actionable resolution options.

**Components:**
- **Embed:** Error message, stack trace (truncated), error classification, suggestions
- **Button Row 1:** 🔄 Retry (Same Config) | 🔧 Retry with Fix | ⏭ Skip Task
- **Button Row 2:** 📝 Edit Task Description | 💬 Add Context | 🔀 Change Agent Profile

**Flow:**
1. Shows full error details with AI-classified error type
2. "Retry with Fix" → opens modal for additional instructions/context to add before retry
3. "Add Context" → modal to add hints (e.g., "use Python 3.11", "avoid dependency X")
4. "Change Agent Profile" → select menu of available profiles

**Effort:** Medium (2–3 hours). Error data already available.

#### `/error-dashboard` — Error Overview Panel
**Purpose:** Show all failed/blocked tasks with quick actions.

**Components:**
- **Embed:** Summary counts by error type, list of recent errors
- **Select Menu:** Pick a failed task to inspect
- **Button Row:** 🔄 Retry All | ⏭ Skip All Blocked | 🔄 Refresh

**Effort:** Low-Medium (1–2 hours).

### 3.4 Project Configuration

#### `/project-settings <project_id>` — Interactive Settings Panel
**Purpose:** Configure project settings through an interactive UI instead of multiple commands.

**Components:**
- **Embed:** Current settings overview (name, branch, max_concurrent, budget, profiles)
- **Button Row 1:** 📝 Edit Name/Description | 🌿 Set Branch | 👥 Concurrency
- **Button Row 2:** 💰 Budget Settings | 🤖 Default Profile | ⏸ Pause/Resume
- **Button Row 3:** 📂 Workspace Settings | 🔔 Notification Settings

**Flow:**
Each button opens an appropriate modal or select menu:
- "Concurrency" → modal with number input (1–10)
- "Default Profile" → select menu of available profiles
- "Budget Settings" → modal with daily token limit

**Effort:** Medium (2–3 hours). Settings already exist; this wraps them in UI.

### 3.5 Quick Actions & Dashboards

#### `/dashboard` — Enhanced Interactive Dashboard
**Purpose:** One-stop overview with inline actions (upgrade existing `/menu`).

**Components:**
- **Embed 1:** System health (agents online, tasks running, queue depth)
- **Embed 2:** Per-project summary with task counts by status
- **Button Row 1:** 🔄 Refresh | ➕ New Task | 📊 Usage Stats
- **Button Row 2:** ⚡ Quick Actions (dropdown)
- **Select Menu:** Quick action picker (Pause All, Resume All, Retry Failed, etc.)

**Effort:** Medium (2–3 hours). Extends existing MenuView.

#### `/quick-task` — Streamlined Task Creation
**Purpose:** One-click task creation with smart defaults.

**Components:**
- **Modal:** Title, Description (optional), Task Type select
- **Follow-up View:** Priority select + Requires Approval toggle + Submit

**Flow:**
1. Opens modal with title + description fields
2. On submit, shows follow-up with priority/approval buttons
3. Creates task with defaults for unspecified fields

**Effort:** Low (1–2 hours). Simplification of existing add-task.

#### `/action-queue` — Pending Actions Inbox
**Purpose:** Show all items requiring human attention in one place.

**Components:**
- **Embed:** Categorized list:
  - 🟡 Awaiting Approval (N tasks)
  - 🔴 Failed/Blocked (N tasks)
  - ❓ Agent Questions (N tasks)
  - ⏸ Paused (N tasks)
- **Select Menu:** Pick an item to act on
- **Button Row:** 🔄 Refresh | ✅ Approve All | ⏭ Skip All Blocked

**Flow:**
1. Aggregates all human-attention-required items
2. Selecting an item shows detail embed with action buttons
3. "Approve All" confirms with a danger-style button before executing

**Effort:** Medium (2–3 hours).

### 3.6 Workflow Automation Configuration

#### `/workflow-rules` — Interactive Rule Builder
**Purpose:** Configure automated responses to events without writing hook YAML.

**Components:**
- **Embed:** List of existing rules
- **Button Row:** ➕ New Rule | ✏️ Edit | 🗑️ Delete
- **Modal (New Rule):** Event trigger select → Action select → Condition input

**Flow:**
1. Shows existing automation rules (hooks)
2. "New Rule" opens a simplified wizard:
   - Step 1: Select trigger (Task Failed, Task Completed, PR Created, etc.)
   - Step 2: Select action (Retry, Notify, Create Follow-up Task, etc.)
   - Step 3: Set conditions (project filter, max retries, etc.)
3. Creates a hook with appropriate configuration

**Effort:** High (4–5 hours). Requires abstracting hook creation.

### 3.7 Dependency Management

#### `/dep-graph <project_id>` — Interactive Dependency Graph
**Purpose:** Visualize and manage task dependencies.

**Components:**
- **Embed:** ASCII/text-rendered dependency tree with status indicators
- **Select Menu:** Pick a task to focus on
- **Button Row:** ➕ Add Dependency | ➖ Remove Dependency | 🔍 Check Health

**Flow:**
1. Renders dependency graph as indented tree with status emojis
2. Selecting a task shows its upstream/downstream dependencies
3. "Add Dependency" → two-step select: pick source task → pick dependency target
4. "Check Health" → runs chain health analysis, highlights issues

**Effort:** Medium (2–3 hours). Tree rendering exists; adding interactive management.

---

## 4. Implementation Plan

### Phase 1: Core Interaction Framework (Estimated: 3–4 hours)

**Goal:** Establish reusable patterns for multi-step human-in-the-loop flows.

**Work items:**
1. **Create `src/discord/interactions.py`** — Shared base classes:
   - `PaginatedView`: Base view with prev/next navigation, page state tracking
   - `ConfirmationView`: Reusable "Are you sure?" with confirm/cancel buttons
   - `MultiStepView`: Base class for wizard-like flows with back/next/cancel
   - `TaskActionView`: Common task action buttons (approve, reject, retry, skip)
2. **Standardize custom_id format:** `{command}:{action}:{entity_type}:{entity_id}:{extra}`
   - Example: `review:approve:task:abc123`
   - Example: `triage:set_priority:task:abc123:p2`
3. **Add interaction state manager:** Lightweight in-memory store for multi-step flow state (with TTL cleanup).

### Phase 2: Task Review & Approval Enhancement (Estimated: 3–4 hours)

**Goal:** Rich task review with inline actions.

**Work items:**
1. Implement `/review-task` command with full action button set
2. Implement `/batch-approve` for bulk operations
3. Enhance existing `TaskApprovalView` with "Request Changes" and "Reject" options
4. Add ephemeral diff viewer with pagination for large diffs

### Phase 3: Error Resolution & Recovery (Estimated: 2–3 hours)

**Goal:** Interactive error handling flows.

**Work items:**
1. Implement `/resolve-error` with context-aware resolution options
2. Implement `/error-dashboard` with aggregated error view
3. Add "Retry with Additional Context" modal flow
4. Wire up agent profile switching on retry

### Phase 4: Action Queue & Triage (Estimated: 3–4 hours)

**Goal:** Centralized human attention inbox and task triage.

**Work items:**
1. Implement `/action-queue` — unified pending actions view
2. Implement `/triage` — sequential task review with priority assignment
3. Add notification badges / counts in dashboard embed
4. Implement batch operations with progress feedback

### Phase 5: Project Settings & Dashboard (Estimated: 2–3 hours)

**Goal:** Interactive configuration panels.

**Work items:**
1. Implement `/project-settings` with modal-based editors
2. Enhance `/dashboard` (upgrade from MenuView) with real-time stats
3. Add `/quick-task` streamlined creation flow

### Phase 6: Dependency Management & Workflows (Estimated: 3–4 hours)

**Goal:** Visual dependency management and simplified automation rules.

**Work items:**
1. Implement `/dep-graph` with interactive tree + management
2. Implement `/workflow-rules` simplified hook builder
3. Add dependency conflict warnings in task creation flow

### Total Estimated Effort: 16–22 hours across all phases

### Priority Ranking:
1. **Phase 2** (Task Review) — Highest value, most common human interaction
2. **Phase 4** (Action Queue) — Reduces context-switching, surfaces urgent items
3. **Phase 3** (Error Resolution) — Speeds up failure recovery
4. **Phase 1** (Framework) — Can be built incrementally alongside Phase 2
5. **Phase 5** (Settings/Dashboard) — Quality of life improvement
6. **Phase 6** (Dependencies/Workflows) — Power user features

---

## 5. Discord API Limitations & Constraints

### Hard Limits (API-enforced)

| Constraint | Limit | Impact |
|------------|-------|--------|
| Components per action row | 5 (buttons) or 1 (select) | Design around 5-row max |
| Action rows per message | 5 | Max 25 buttons or 5 selects per message |
| Select menu options | 25 | Must paginate longer lists |
| Modal text inputs | 5 | Keep forms concise |
| TextInput max_length | 4000 chars | Limit for task descriptions in modals |
| Embed total chars | 6000 | Already handled by `make_embed()` auto-truncation |
| Embed fields | 25 | Paginate large field lists |
| Custom ID length | 100 chars | Limit entity IDs in custom_id format |
| Interaction response time | 3 seconds | Must defer for slow operations |
| Modal timeout | 15 minutes | User must submit within this window |
| Autocomplete response time | 3 seconds | Keep autocomplete queries fast |
| Message content | 2000 chars | Use embeds for longer content |

### Behavioral Constraints

1. **Modals cannot be chained directly.** A modal submission cannot trigger another modal. Instead, send a message with a button that opens the next modal.

2. **Interaction tokens expire after 15 minutes.** For long-running flows, send a new message rather than editing the original interaction response.

3. **Ephemeral messages cannot be edited after the interaction token expires.** Use regular messages for long-lived interactive content.

4. **Views with `timeout=None` survive bot restarts** but require `bot.add_view()` on startup to re-register callbacks. Already handled in `on_ready()`.

5. **Select menus with dynamic options** must be rebuilt each time — cannot modify options after sending. For search/filter UIs, send new messages.

6. **Rate limits:**
   - Global: 50 requests/second
   - Per-channel: ~5 messages/second
   - Interaction responses: must respond within 3 seconds (use `defer()` for slow ops)
   - Webhook follow-ups: 5/second

7. **Component interactions are per-message.** A View's buttons only work on the message they were sent with. Old messages' buttons stop working after the View times out.

8. **No nested components.** Can't put a select inside a button group or vice versa. Each component type takes specific row positions.

9. **Link buttons don't fire callbacks.** They open URLs directly in the browser. Useful for PR links but can't track clicks.

### Design Implications

- **Prefer buttons over selects** when options ≤ 5 for faster interaction (one click vs. open menu + select + close).
- **Use `defer()` aggressively** for any operation that might take > 1 second (database queries, git operations, API calls).
- **Paginate with new messages** rather than editing for lists > 25 items.
- **Use ephemeral messages** for sensitive data, error details, and user-specific confirmations.
- **Chain modal → message → modal** (not modal → modal) for multi-step forms.
- **Set appropriate View timeouts**: 1 hour for interactive sessions, 24 hours for approval flows, None for dashboards.

---

## 6. Example Code Snippets

### 6.1 Rich Task Review View

```python
class TaskReviewView(discord.ui.View):
    """Interactive task review panel with inline actions."""

    def __init__(self, task: Task, handler: CommandHandler):
        super().__init__(timeout=3600)  # 1 hour
        self.task = task
        self.handler = handler

        # Add PR link button if available
        if task.pr_url:
            self.add_item(discord.ui.Button(
                label="View PR",
                style=discord.ButtonStyle.link,
                url=task.pr_url,
                emoji="🔗",
                row=0,
            ))

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅", row=1)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        result = await self.handler.execute("approve_task", {"task_id": self.task.id})
        await interaction.followup.send(
            embed=success_embed("Task Approved", f"Task `{self.task.id}` has been approved."),
            ephemeral=True,
        )
        self._disable_all()
        await interaction.message.edit(view=self)

    @discord.ui.button(label="Request Changes", style=discord.ButtonStyle.primary, emoji="📝", row=1)
    async def request_changes(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = FeedbackModal(title="Request Changes", task=self.task, handler=self.handler)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, emoji="❌", row=1)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = RejectionModal(title="Reject Task", task=self.task, handler=self.handler)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="View Diff", style=discord.ButtonStyle.secondary, emoji="📋", row=2)
    async def view_diff(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        result = await self.handler.execute("task_diff", {"task_id": self.task.id})
        diff_text = result.get("diff", "No changes found.")
        # Truncate for Discord message limit
        if len(diff_text) > 1900:
            diff_text = diff_text[:1900] + "\n... (truncated)"
        await interaction.followup.send(f"```diff\n{diff_text}\n```", ephemeral=True)

    @discord.ui.button(label="Add Context", style=discord.ButtonStyle.secondary, emoji="💬", row=2)
    async def add_context(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = AddContextModal(task=self.task, handler=self.handler)
        await interaction.response.send_modal(modal)

    def _disable_all(self):
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True


class FeedbackModal(discord.ui.Modal, title="Request Changes"):
    feedback = discord.ui.TextInput(
        label="What changes are needed?",
        style=discord.TextStyle.long,
        placeholder="Describe the changes you'd like the agent to make...",
        required=True,
        max_length=2000,
    )

    def __init__(self, task: Task, handler: CommandHandler, **kwargs):
        super().__init__(**kwargs)
        self.task = task
        self.handler = handler

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await self.handler.execute("reopen_with_feedback", {
            "task_id": self.task.id,
            "feedback": self.feedback.value,
        })
        await interaction.followup.send(
            embed=info_embed("Changes Requested",
                f"Task `{self.task.id}` reopened with feedback."),
            ephemeral=True,
        )
```

### 6.2 Action Queue (Pending Human Actions)

```python
class ActionQueueView(discord.ui.View):
    """Unified view of all items requiring human attention."""

    def __init__(self, handler: CommandHandler, items: dict):
        super().__init__(timeout=3600)
        self.handler = handler
        self.items = items  # {category: [tasks]}

        # Build select menu with pending items
        options = []
        for category, tasks in items.items():
            for task in tasks[:8]:  # Cap per category to stay under 25
                emoji = {"approval": "🟡", "failed": "🔴",
                         "question": "❓", "paused": "⏸"}.get(category, "📋")
                options.append(discord.SelectOption(
                    label=f"{task.title[:50]}",
                    value=f"{category}:{task.id}",
                    description=f"{task.project_id} — {task.status.value}",
                    emoji=emoji,
                ))

        if options:
            select = discord.ui.Select(
                placeholder="Select an item to act on...",
                options=options[:25],
                row=0,
            )
            select.callback = self.on_select
            self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        category, task_id = interaction.data["values"][0].split(":", 1)
        await interaction.response.defer(ephemeral=True)

        result = await self.handler.execute("get_task", {"task_id": task_id})
        task = result.get("task")
        if not task:
            await interaction.followup.send("Task not found.", ephemeral=True)
            return

        # Show task-specific action panel
        view = TaskReviewView(task=task, handler=self.handler)
        embed = _build_task_detail_embed(task)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @discord.ui.button(label="Approve All Pending", style=discord.ButtonStyle.success,
                       emoji="✅", row=1)
    async def approve_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Confirmation step
        confirm_view = ConfirmationView(
            on_confirm=self._do_approve_all,
            message=f"Approve {len(self.items.get('approval', []))} tasks?",
        )
        await interaction.response.send_message(
            embed=warning_embed("Confirm Batch Approve",
                f"This will approve **{len(self.items.get('approval', []))}** tasks."),
            view=confirm_view,
            ephemeral=True,
        )

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary,
                       emoji="🔄", row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        # Rebuild and resend
        ...
```

### 6.3 Paginated View Base Class

```python
class PaginatedView(discord.ui.View):
    """Base class for paginated interactive views."""

    def __init__(self, items: list, per_page: int = 10, timeout: int = 3600):
        super().__init__(timeout=timeout)
        self.items = items
        self.per_page = per_page
        self.page = 0
        self.total_pages = max(1, (len(items) + per_page - 1) // per_page)

    @property
    def current_page_items(self) -> list:
        start = self.page * self.per_page
        return self.items[start:start + self.per_page]

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.secondary, row=4)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await self.update_page(interaction)

    @discord.ui.button(label="Page 1/1", style=discord.ButtonStyle.secondary,
                       disabled=True, row=4)
    async def page_indicator(self, interaction: discord.Interaction,
                             button: discord.ui.Button):
        pass  # Display only

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=4)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.total_pages - 1:
            self.page += 1
        await self.update_page(interaction)

    async def update_page(self, interaction: discord.Interaction):
        self.page_indicator.label = f"Page {self.page + 1}/{self.total_pages}"
        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= self.total_pages - 1
        embed = await self.build_page_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def build_page_embed(self) -> discord.Embed:
        """Override in subclass to build the embed for the current page."""
        raise NotImplementedError
```

### 6.4 Confirmation Dialog Pattern

```python
class ConfirmationView(discord.ui.View):
    """Reusable confirmation dialog with confirm/cancel buttons."""

    def __init__(self, on_confirm, message: str = "Are you sure?",
                 confirm_label: str = "Confirm", danger: bool = False):
        super().__init__(timeout=300)  # 5 min
        self.on_confirm = on_confirm
        self.message = message
        self.confirm_button.label = confirm_label
        if danger:
            self.confirm_button.style = discord.ButtonStyle.danger

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success, emoji="✅")
    async def confirm_button(self, interaction: discord.Interaction,
                             button: discord.ui.Button):
        self._disable_all()
        await interaction.response.edit_message(view=self)
        await self.on_confirm(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="❌")
    async def cancel_button(self, interaction: discord.Interaction,
                            button: discord.ui.Button):
        self._disable_all()
        await interaction.response.edit_message(
            content="Cancelled.", view=self, embed=None)

    def _disable_all(self):
        for item in self.children:
            item.disabled = True
```

### 6.5 Custom ID Convention Proposal

```python
# Proposed custom_id format:
# {command}:{action}:{entity_type}:{entity_id}[:{extra}]
#
# Examples:
#   review:approve:task:abc123
#   review:reject:task:abc123
#   triage:priority:task:abc123:p2
#   action_queue:select:task:abc123
#   batch:approve:task:abc123
#   settings:edit:project:myproj:branch
#   depgraph:remove:dep:abc123:def456
#
# Parsing utility:

def parse_custom_id(custom_id: str) -> dict:
    """Parse a structured custom_id into components."""
    parts = custom_id.split(":")
    result = {"command": parts[0]}
    if len(parts) > 1:
        result["action"] = parts[1]
    if len(parts) > 2:
        result["entity_type"] = parts[2]
    if len(parts) > 3:
        result["entity_id"] = parts[3]
    if len(parts) > 4:
        result["extra"] = ":".join(parts[4:])  # Rejoin remaining
    return result
```

---

## Summary

### What We Already Have
- Comprehensive Discord UI infrastructure (85+ commands, modals, buttons, selects, embeds)
- Task lifecycle with approval flow (AWAITING_APPROVAL state, PR integration)
- Agent question/answer flow (WAITING_INPUT state)
- Persistent dashboard (MenuView)
- Hook-based automation (periodic + event-driven)
- Rich embed formatting with auto-truncation

### Key Gaps to Fill
1. **No unified "action inbox"** — users must check multiple commands to find pending items
2. **No batch operations** — approval/rejection is one-at-a-time
3. **Limited error recovery UI** — errors show buttons but no "retry with additional context" flow
4. **No task triage workflow** — new tasks just go into the queue without structured review
5. **No interactive settings panel** — project configuration requires knowing multiple commands
6. **No dependency visualization** — dependency graph only available as text output

### Recommended Approach
Start with **Phase 2** (Task Review) and **Phase 4** (Action Queue) as they deliver the highest value. The framework components (Phase 1) should be built organically as needed by these features rather than as a standalone effort. Phases 3, 5, and 6 can follow based on user feedback.
