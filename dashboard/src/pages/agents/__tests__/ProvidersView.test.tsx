import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type {
  ProviderAllocationApplyResponse,
  ProviderAllocationGroup,
  ProviderAllocationPreviewResponse,
  ProviderAllocationPreviewSession,
  ProviderAllocationProfile,
  ProviderAllocationStatusResponse,
} from "../../../api/client";
import ProvidersView from "../ProvidersView";

const api = vi.hoisted(() => ({
  getProviderAllocationApiProvidersAllocationGet: vi.fn(),
  postProviderAllocationPreviewApiProvidersAllocationPreviewPost: vi.fn(),
  postProviderAllocationApplyApiProvidersAllocationApplyPost: vi.fn(),
}));
vi.mock("../../../api/client", () => api);

/** Mirrors one ordinary worker profile of ``provider_allocation_status`` (src/api/models/provider.py). */
function profile(profileId: string, over: Partial<ProviderAllocationProfile> = {}): ProviderAllocationProfile {
  return {
    profile_id: profileId, name: profileId, harness: profileId.endsWith("codex") ? "codex" : "claude",
    lifecycle: "pool", enabled: true, intelligence_class: "standard-high",
    min_active: 0, max_active: 2, min_per_project: null,
    supply: { ready: 0, idle: 0, busy: 0, starting: 0, draining: 0, unresponsive: 0 },
    projects: [], sessions: [],
    pinned: { count: 0, by_status: {}, task_ids: [] },
    preferred: { count: 0, by_status: {}, task_ids: [] },
    hidden: { projects: 0, sessions: 0, tasks: 0 },
    ...over,
  };
}

/**
 * A mixed fleet: Claude and Codex worker rungs at two classes, pool and task
 * lifecycles, idle, busy, starting and draining sessions in two projects,
 * pins on both providers and manual agents with and without overrides.
 */
function fleet(): ProviderAllocationStatusResponse {
  const claude: ProviderAllocationGroup = {
    provider: "claude", vendor: "anthropic", state: "available", harnesses: ["claude"],
    supply: { ready: 3, idle: 1, busy: 1, starting: 0, draining: 1, unresponsive: 0 },
    ceiling: { min_active: 1, max_active: 6, unbounded: false, pool_profiles: 2 },
    profiles: [
      profile("deep-claude", { lifecycle: "task", intelligence_class: "deep", min_active: null, max_active: null,
        supply: { ready: null, idle: 0, busy: 0, starting: 0, draining: 0, unresponsive: 0 } }),
      profile("fast-medium-claude", { intelligence_class: "fast-medium", min_active: 0, max_active: 2,
        supply: { ready: 0, idle: 0, busy: 0, starting: 0, draining: 1, unresponsive: 0 },
        sessions: [{ session_id: "claude-draining", project_id: "other-project", lifecycle: "pool", state: "running", activity: "draining" }] }),
      profile("standard-high-claude", { min_active: 1, max_active: 4,
        supply: { ready: 3, idle: 1, busy: 1, starting: 0, draining: 0, unresponsive: 0 },
        projects: [
          { project_id: "agent-queue", ready: 3, idle: 0, busy: 1, starting: 0, draining: 0, unresponsive: 0 },
          { project_id: "other-project", ready: 0, idle: 1, busy: 0, starting: 0, draining: 0, unresponsive: 0 },
        ],
        sessions: [
          { session_id: "claude-busy", project_id: "agent-queue", lifecycle: "pool", state: "running", activity: "busy", task_id: "t-claude", task_title: "Claude work" },
          { session_id: "claude-idle", project_id: "other-project", lifecycle: "pool", state: "running", activity: "idle", idle_seconds: 120 },
        ],
        pinned: { count: 1, by_status: { READY: 1 }, task_ids: ["pin-claude"] } }),
    ],
    manual_agents: [{ agent_id: "agent-builder", name: "Builder", profile_id: "deep-claude", enabled: true, state: "idle",
      has_overrides: false, effective_harness: "claude", effective_class: "deep" }],
    pinned_tasks: 1, preferred_tasks: 0, last_allocation: null,
  };
  const codex: ProviderAllocationGroup = {
    provider: "codex", vendor: "openai", state: "degraded", harnesses: ["codex"],
    supply: { ready: 2, idle: 0, busy: 2, starting: 1, draining: 0, unresponsive: 0 },
    ceiling: { min_active: 0, max_active: null, unbounded: true, pool_profiles: 2 },
    profiles: [
      profile("fast-medium-codex", { intelligence_class: "fast-medium", max_active: null }),
      profile("standard-high-codex", { min_active: 0, max_active: 3,
        supply: { ready: 2, idle: 0, busy: 2, starting: 1, draining: 0, unresponsive: 0 },
        projects: [
          { project_id: "agent-queue", ready: 2, idle: 0, busy: 1, starting: 1, draining: 0, unresponsive: 0 },
          { project_id: "other-project", ready: 0, idle: 0, busy: 1, starting: 0, draining: 0, unresponsive: 0 },
        ],
        sessions: [
          { session_id: "codex-busy-1", project_id: "agent-queue", lifecycle: "pool", state: "running", activity: "busy", task_id: "t-codex-1", task_title: "Codex one" },
          { session_id: "codex-busy-2", project_id: "other-project", lifecycle: "pool", state: "running", activity: "busy", task_id: "t-codex-2", task_title: "Codex two" },
          { session_id: "codex-starting", project_id: "agent-queue", lifecycle: "pool", state: "starting", activity: "starting" },
        ],
        pinned: { count: 2, by_status: { READY: 1, IN_PROGRESS: 1 }, task_ids: ["pin-codex-1", "pin-codex-2"] },
        preferred: { count: 1, by_status: { READY: 1 }, task_ids: ["pref-codex"] } }),
    ],
    manual_agents: [{ agent_id: "agent-reviewer", name: "Reviewer", profile_id: "standard-high-codex", enabled: true,
      state: "busy", harness: "codex", model: "gpt-5.5", has_overrides: true, effective_harness: "codex",
      current_task_id: "t-review" }],
    pinned_tasks: 2, preferred_tasks: 1,
    last_allocation: { event_id: 41, at: 900, request_id: "alloc-previous", status: "applied", actor: "operator" },
  };
  return {
    success: true, now: 1_000, redacted: false, global_max_active: 12,
    providers: [claude, codex],
    projects: [
      { project_id: "agent-queue", name: "Agent Queue", status: "ACTIVE", preferred_provider: "codex", max_concurrent_agents: 4 },
      { project_id: "other-project", name: "Other", status: "ACTIVE", preferred_provider: null, max_concurrent_agents: 2 },
    ],
    diagnostics: [{ kind: "profile", id: "supervisor", harness: "claude", lifecycle: "task", provider: "claude", reason: "role" }],
  };
}

function session(sessionId: string, over: Partial<ProviderAllocationPreviewSession> = {}): ProviderAllocationPreviewSession {
  return {
    session_id: sessionId, project_id: "agent-queue", profile_id: "standard-high-codex",
    lifecycle: "pool", state: "running", activity: "busy", action: "stop_after_task", ...over,
  };
}

/** Taking the Codex rungs out of the pool while two of their workers are busy. */
function preview(over: Partial<ProviderAllocationPreviewResponse> = {}): ProviderAllocationPreviewResponse {
  const drain = over.request?.drain ?? "graceful";
  const action = drain === "interrupt-busy" ? "interrupt" : "stop_after_task";
  return {
    success: true, now: 1_000, provider: "codex", vendor: "openai", state: "degraded",
    request: { provider: "codex", profile_ids: null, participation: "task", bounds: null, receive_new_work: null, drain, allow_pinned_wait: false },
    required_scope: "operator", global_max_active: 12,
    selected: ["fast-medium-codex", "standard-high-codex"],
    profiles: [
      { profile_id: "fast-medium-codex", name: "fast-medium-codex", harness: "codex", intelligence_class: "fast-medium", selected: true, changed: true,
        changed_fields: ["lifecycle"], before: { lifecycle: "pool", enabled: true, min_active: 0, max_active: null },
        after: { lifecycle: "task", enabled: true, min_active: null, max_active: null } },
      { profile_id: "standard-high-codex", name: "standard-high-codex", harness: "codex", intelligence_class: "standard-high", selected: true, changed: true,
        changed_fields: ["lifecycle", "max_active"], before: { lifecycle: "pool", enabled: true, min_active: 0, max_active: 3 },
        after: { lifecycle: "task", enabled: true, min_active: null, max_active: null } },
    ],
    ceiling: { before: { min_active: 0, max_active: null, unbounded: true, pool_profiles: 2 }, after: { min_active: 0, max_active: 0, unbounded: false, pool_profiles: 0 } },
    project_limits: [],
    sessions: [
      session("codex-busy-1", { task_id: "t-codex-1", task_title: "Codex one", action }),
      session("codex-busy-2", { project_id: "other-project", task_id: "t-codex-2", task_title: "Codex two", action }),
      session("codex-starting", { state: "starting", activity: "starting", action: "stop" }),
    ],
    busy: { session_ids: ["codex-busy-1", "codex-busy-2"], task_ids: ["t-codex-1", "t-codex-2"] },
    pinned: [], manual_agents: [], preference: null, warnings: [], blocked: false,
    preview_token: "tok-1",
    ...over,
  };
}

function applied(over: Partial<ProviderAllocationApplyResponse> = {}): ProviderAllocationApplyResponse {
  return {
    success: true, status: "applied", request_id: "alloc-new", event_id: 42, provider: "codex", vendor: "openai",
    actor: "operator", preview_token: "tok-1",
    profiles: [
      { profile_id: "fast-medium-codex", status: "applied", changed_fields: ["lifecycle"],
        before: { lifecycle: "pool" }, after: { lifecycle: "task" } },
      { profile_id: "standard-high-codex", status: "applied", changed_fields: ["lifecycle", "max_active"],
        before: { lifecycle: "pool" }, after: { lifecycle: "task" } },
    ],
    session_actions: [{ session_id: "codex-starting", profile_id: "standard-high-codex", action: "drain" }],
    ...over,
  };
}

/** What the client interceptor throws for a non-2xx answer (src/api/client.ts). */
function rejected(status: number, payload: Record<string, unknown>) {
  const error = new Error(`API ${status}: ${String(payload.error ?? "")}`) as Error & { payload?: unknown };
  error.payload = payload;
  return error;
}

const clients: QueryClient[] = [];

function renderView() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  clients.push(client);
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/agents?view=providers"]}>
        <ProvidersView />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function openEditor(label = "OpenAI (Codex)") {
  renderView();
  fireEvent.click(await screen.findByRole("button", { name: "Edit " + label + " allocation" }));
  return screen.getByRole("dialog", { name: label + " allocation" });
}

async function previewTakingCodexOutOfPool(drain?: "idle-now" | "interrupt-busy") {
  const drawer = await openEditor();
  fireEvent.click(within(drawer).getByRole("radio", { name: /Task — stop pooling/ }));
  if (drain === "interrupt-busy") {
    fireEvent.click(within(drawer).getByText("Danger: interrupt busy work"));
    fireEvent.click(within(drawer).getByRole("radio", { name: /Interrupt busy work/ }));
  } else if (drain === "idle-now") {
    fireEvent.click(within(drawer).getByRole("radio", { name: /Stop idle workers now/ }));
  }
  fireEvent.click(within(drawer).getByRole("button", { name: "Preview" }));
  await within(drawer).findByRole("heading", { name: "Review before applying" });
  return drawer;
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getProviderAllocationApiProvidersAllocationGet.mockResolvedValue({ data: fleet() });
  api.postProviderAllocationPreviewApiProvidersAllocationPreviewPost.mockResolvedValue({ data: preview() });
  api.postProviderAllocationApplyApiProvidersAllocationApplyPost.mockResolvedValue({ data: applied() });
});

afterEach(() => {
  cleanup();
  clients.splice(0).forEach((client) => client.clear());
});

describe("ProvidersView — provider cards", () => {
  it("renders a mixed fleet as one card per provider with ceiling, supply, bounds and routing", async () => {
    renderView();
    const claude = await screen.findByRole("region", { name: "Anthropic (Claude) provider" });
    const codex = screen.getByRole("region", { name: "OpenAI (Codex) provider" });

    // The aggregate ceiling is read-only and never mistaken for one bound.
    expect(within(claude).getByText(/Pool ceiling 1–6 across 2 pool profiles/)).toBeInTheDocument();
    expect(within(codex).getByText(/Pool ceiling unbounded across 2 pool profiles/)).toBeInTheDocument();
    expect(within(codex).getByText("Degraded")).toBeInTheDocument();

    // Supply is the server's buckets, verbatim.
    const supply = within(codex).getByLabelText("OpenAI (Codex) supply");
    expect(supply).toHaveTextContent("live 3");
    expect(supply).toHaveTextContent("busy 2");
    expect(supply).toHaveTextContent("starting 1");
    expect(within(claude).getByLabelText("Anthropic (Claude) supply")).toHaveTextContent("draining 1");

    // Every ordinary worker profile with its own per-profile bounds.
    const profiles = within(claude).getByRole("list", { name: "Anthropic (Claude) profiles" });
    expect(within(profiles).getAllByRole("listitem").map((row) => row.getAttribute("data-profile-id")))
      .toEqual(["deep-claude", "fast-medium-claude", "standard-high-claude"]);
    const standard = within(profiles).getByText("standard-high-claude").closest("li")!;
    expect(standard).toHaveTextContent("[1–4]");
    expect(standard).toHaveTextContent("Pool");
    expect(within(profiles).getByText("deep-claude").closest("li")).toHaveTextContent("Task");
    expect(within(codex).getByText("fast-medium-codex").closest("li")).toHaveTextContent("[0–∞]");

    // Projects and their preference for unpinned work.
    expect(within(codex).getByText("agent-queue").closest("li")).toHaveTextContent("prefers this provider");
    expect(within(claude).getByText("agent-queue").closest("li")).toHaveTextContent("prefers Codex");
    expect(within(claude).getByText("other-project").closest("li")).toHaveTextContent("no preference");

    // What bulk controls never rewrite.
    expect(within(codex).getByText("2 pinned tasks")).toBeInTheDocument();
    expect(within(codex).getByText("1 manual agent")).toBeInTheDocument();
    expect(within(claude).getByText("1 pinned task")).toBeInTheDocument();

    // The last allocation links to its events.
    expect(within(codex).getByRole("link", { name: /alloc-previous/ }).getAttribute("href")).toContain("eventRequest=alloc-previous");
    expect(screen.getByText(/1 profile not managed by bulk allocation/)).toBeInTheDocument();
  });

  it("shows the daemon's refusal when the status cannot be read", async () => {
    api.getProviderAllocationApiProvidersAllocationGet.mockRejectedValue(rejected(503, { error: "orchestrator not ready" }));
    renderView();
    // The status read retries once (its own ``retry: 1``) before it reports.
    expect(await screen.findByRole("alert", {}, { timeout: 4_000 })).toHaveTextContent("orchestrator not ready");
  });
});

describe("ProvidersView — preview then apply", () => {
  it("previews with a graceful drain, expands a busy confirmation and applies the reviewed token", async () => {
    const drawer = await previewTakingCodexOutOfPool();

    expect(api.postProviderAllocationPreviewApiProvidersAllocationPreviewPost).toHaveBeenCalledWith(
      expect.objectContaining({ body: { provider: "codex", participation: "task", drain: "graceful" } }),
    );
    // Any busy worker opens the affected profiles and sessions by default.
    const profiles = within(drawer).getByText("Profiles (2 changing)").closest("details")!;
    const sessions = within(drawer).getByText("Sessions (3 affected, 2 busy)").closest("details")!;
    expect(profiles).toHaveAttribute("open");
    expect(sessions).toHaveAttribute("open");
    expect(within(sessions).getByText("codex-busy-1").closest("li")).toHaveTextContent("finishes its task, then stops");
    expect(within(drawer).getByText(/Provider ceiling unbounded → 0/)).toBeInTheDocument();
    // Graceful never asks for the danger confirmation.
    expect(within(drawer).queryByLabelText(/Type 2 to interrupt/)).not.toBeInTheDocument();

    fireEvent.click(within(drawer).getByRole("button", { name: "Apply" }));

    expect(await within(drawer).findByRole("status")).toHaveTextContent("Allocation applied");
    expect(api.postProviderAllocationApplyApiProvidersAllocationApplyPost).toHaveBeenCalledWith(
      expect.objectContaining({ body: { preview_token: "tok-1" } }),
    );
    const events = within(drawer).getByRole("link", { name: /alloc-new/ });
    expect(events.getAttribute("href")).toContain("eventRequest=alloc-new");
    expect(events.getAttribute("href")).toContain("openDrawer=events");
    // The Events drawer opens under this overlay, so following the link closes it.
    fireEvent.click(events);
    expect(screen.queryByRole("dialog", { name: "OpenAI (Codex) allocation" })).not.toBeInTheDocument();
  });

  it("collapses the confirmation when no worker is busy", async () => {
    api.postProviderAllocationPreviewApiProvidersAllocationPreviewPost.mockResolvedValue({ data: preview({
      sessions: [session("codex-starting", { state: "starting", activity: "starting", action: "stop" })],
      busy: { session_ids: [], task_ids: [] },
    }) });
    const drawer = await previewTakingCodexOutOfPool();
    expect(within(drawer).getByText("Profiles (2 changing)").closest("details")).not.toHaveAttribute("open");
    expect(within(drawer).getByText("Sessions (1 affected, 0 busy)").closest("details")).not.toHaveAttribute("open");
  });

  it("keeps interrupt-busy behind a typed busy count that lists the exact preview set", async () => {
    api.postProviderAllocationPreviewApiProvidersAllocationPreviewPost.mockResolvedValue({ data: preview({
      request: { provider: "codex", profile_ids: null, participation: "task", bounds: null, receive_new_work: null, drain: "interrupt-busy", allow_pinned_wait: false },
    }) });
    const drawer = await previewTakingCodexOutOfPool("interrupt-busy");
    expect(api.postProviderAllocationPreviewApiProvidersAllocationPreviewPost).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.objectContaining({ drain: "interrupt-busy" }) }),
    );

    const danger = within(drawer).getByRole("group", { name: "Interrupt busy work" });
    const listed = within(danger).getAllByRole("listitem").map((row) => row.textContent);
    expect(listed).toEqual([expect.stringContaining("codex-busy-1"), expect.stringContaining("codex-busy-2")]);
    expect(listed[0]).toContain("t-codex-1");

    const apply = within(drawer).getByRole("button", { name: "Interrupt 2 busy tasks and apply" });
    expect(apply).toBeDisabled();
    const typed = within(danger).getByLabelText("Type 2 to interrupt these busy tasks");
    fireEvent.change(typed, { target: { value: "1" } });
    expect(apply).toBeDisabled();
    fireEvent.change(typed, { target: { value: "2" } });
    expect(apply).toBeEnabled();

    fireEvent.click(apply);
    await within(drawer).findByRole("status");
    expect(api.postProviderAllocationApplyApiProvidersAllocationApplyPost).toHaveBeenCalledWith(
      expect.objectContaining({ body: { preview_token: "tok-1", authorize_busy_interrupt: ["codex-busy-1", "codex-busy-2"] } }),
    );
  });

  it("requires acknowledging a blocking pinned wait before apply", async () => {
    api.postProviderAllocationPreviewApiProvidersAllocationPreviewPost.mockResolvedValue({ data: preview({
      pinned: [{ task_id: "pin-codex-1", project_id: "agent-queue", profile_id: "standard-high-codex", status: "READY", waits: true }],
      warnings: [{ code: "pinned_ready_wait", blocking: true, acknowledged: false, subjects: ["pin-codex-1"],
        message: "1 pinned READY task(s) stay on codex profiles this request takes out of the pool" }],
      blocked: true,
    }) });
    const drawer = await previewTakingCodexOutOfPool();
    expect(within(drawer).getByText(/1 pinned READY task\(s\) stay on codex profiles/)).toBeInTheDocument();
    const apply = within(drawer).getByRole("button", { name: "Apply" });
    expect(apply).toBeDisabled();

    fireEvent.click(within(drawer).getByRole("checkbox", { name: /Leave the pinned READY tasks waiting/ }));
    fireEvent.click(apply);
    await within(drawer).findByRole("status");
    expect(api.postProviderAllocationApplyApiProvidersAllocationApplyPost).toHaveBeenCalledWith(
      expect.objectContaining({ body: { preview_token: "tok-1", allow_pinned_wait: true } }),
    );
  });

  it("refuses a stale preview and asks for a review of the fresh one", async () => {
    const fresh = preview({
      preview_token: "tok-2",
      sessions: [
        ...preview().sessions!,
        session("codex-busy-3", { task_id: "t-codex-3", task_title: "Codex three" }),
      ],
      busy: { session_ids: ["codex-busy-1", "codex-busy-2", "codex-busy-3"], task_ids: ["t-codex-1", "t-codex-2", "t-codex-3"] },
    });
    api.postProviderAllocationApplyApiProvidersAllocationApplyPost
      .mockRejectedValueOnce(rejected(409, {
        success: false, error_code: "preview_stale",
        error: "preview_stale: the fleet changed since the preview; review the fresh preview and apply its token",
        preview: fresh,
      }))
      .mockResolvedValueOnce({ data: applied({ preview_token: "tok-2" }) });
    const drawer = await previewTakingCodexOutOfPool();

    fireEvent.click(within(drawer).getByRole("button", { name: "Apply" }));

    const alert = await within(drawer).findByRole("alert");
    expect(alert).toHaveTextContent("The fleet changed since this preview");
    expect(within(drawer).queryByText("Allocation applied")).not.toBeInTheDocument();
    // The fresh preview replaces the stale one: the new busy session is listed.
    expect(within(drawer).getByText("codex-busy-3")).toBeInTheDocument();
    expect(within(drawer).getByText("Sessions (4 affected, 3 busy)")).toBeInTheDocument();

    fireEvent.click(within(drawer).getByRole("button", { name: "Apply" }));
    await within(drawer).findByRole("status");
    expect(api.postProviderAllocationApplyApiProvidersAllocationApplyPost).toHaveBeenLastCalledWith(
      expect.objectContaining({ body: { preview_token: "tok-2" } }),
    );
  });

  it("reports a partial apply row by row and never as success", async () => {
    api.postProviderAllocationApplyApiProvidersAllocationApplyPost.mockRejectedValue(rejected(409, {
      success: false, status: "partial", error_code: "allocation_partial",
      error: "provider allocation partial: standard-high-codex: vault write failed",
      request_id: "alloc-broken", provider: "codex", vendor: "openai",
      profiles: [
        { profile_id: "fast-medium-codex", status: "rollback_failed", changed_fields: ["lifecycle"],
          before: { lifecycle: "pool" }, after: { lifecycle: "task" }, compensated: false,
          compensation_error: "profile file locked" },
        { profile_id: "standard-high-codex", status: "failed", changed_fields: ["lifecycle"],
          before: { lifecycle: "pool" }, after: { lifecycle: "task" }, error: "vault write failed" },
      ],
      session_actions: [{ session_id: "codex-starting", action: "drain", error: "session vanished" }],
    }));
    const drawer = await previewTakingCodexOutOfPool();

    fireEvent.click(within(drawer).getByRole("button", { name: "Apply" }));

    const alert = await within(drawer).findByRole("alert");
    expect(alert).toHaveTextContent("Allocation partially applied");
    expect(alert).toHaveTextContent("vault write failed");
    expect(within(drawer).queryByRole("status")).not.toBeInTheDocument();
    expect(within(drawer).queryByText("Allocation applied")).not.toBeInTheDocument();
    const rows = within(drawer).getByRole("list", { name: "Profile results" });
    expect(within(rows).getByText("fast-medium-codex").closest("li")).toHaveTextContent("rollback failed");
    expect(within(rows).getByText("fast-medium-codex").closest("li")).toHaveTextContent("profile file locked");
    expect(within(rows).getByText("standard-high-codex").closest("li")).toHaveTextContent("failed");
    expect(within(drawer).getByText("codex-starting").closest("li")).toHaveTextContent("session vanished");
    expect(within(drawer).getByRole("link", { name: /alloc-broken/ }).getAttribute("href")).toContain("eventRequest=alloc-broken");
  });

  it("reports a rolled-back apply as a failure that changed nothing", async () => {
    api.postProviderAllocationApplyApiProvidersAllocationApplyPost.mockRejectedValue(rejected(409, {
      success: false, status: "rolled_back", error_code: "allocation_rolled_back",
      error: "provider allocation rolled back: standard-high-codex: vault write failed",
      request_id: "alloc-undone",
      profiles: [
        { profile_id: "fast-medium-codex", status: "rolled_back", before: { lifecycle: "pool" }, after: { lifecycle: "task" }, compensated: true },
        { profile_id: "standard-high-codex", status: "failed", before: { lifecycle: "pool" }, after: { lifecycle: "task" }, error: "vault write failed" },
      ],
    }));
    const drawer = await previewTakingCodexOutOfPool();
    fireEvent.click(within(drawer).getByRole("button", { name: "Apply" }));
    const alert = await within(drawer).findByRole("alert");
    expect(alert).toHaveTextContent("Allocation rolled back");
    expect(within(drawer).queryByText("Allocation applied")).not.toBeInTheDocument();
  });

  it("builds a preference-only request for one project", async () => {
    api.postProviderAllocationPreviewApiProvidersAllocationPreviewPost.mockResolvedValue({ data: preview({
      request: { provider: "claude", profile_ids: null, participation: null, bounds: null, receive_new_work: { project_id: "other-project", mode: "prefer" }, drain: "graceful", allow_pinned_wait: false },
      required_scope: "project_admin", profiles: [], sessions: [], busy: { session_ids: [], task_ids: [] },
      preference: { project_id: "other-project", mode: "prefer", before: null, after: "claude", changed: true },
    }) });
    const drawer = await openEditor("Anthropic (Claude)");
    fireEvent.change(within(drawer).getByLabelText("Project"), { target: { value: "other-project" } });
    fireEvent.click(within(drawer).getByRole("radio", { name: /Prefer this provider/ }));
    fireEvent.click(within(drawer).getByRole("button", { name: "Preview" }));
    await within(drawer).findByRole("heading", { name: "Review before applying" });
    expect(api.postProviderAllocationPreviewApiProvidersAllocationPreviewPost).toHaveBeenCalledWith(
      expect.objectContaining({ body: { provider: "claude", receive_new_work: { project_id: "other-project", mode: "prefer" }, drain: "graceful" } }),
    );
    expect(within(drawer).getByText(/other-project: no preference → Claude/)).toBeInTheDocument();
  });

  it("narrows the selection, sends bounds and shows a refused preview in the editor", async () => {
    api.postProviderAllocationPreviewApiProvidersAllocationPreviewPost.mockRejectedValue(
      rejected(403, { error: "out of scope: provider_allocation_preview changes global profiles" }),
    );
    const drawer = await openEditor();
    expect(within(drawer).getByRole("button", { name: "Preview" })).toBeDisabled();
    fireEvent.click(within(drawer).getByRole("checkbox", { name: "fast-medium-codex" }));
    fireEvent.click(within(drawer).getByRole("checkbox", { name: "Change per-profile bounds" }));
    fireEvent.change(within(drawer).getByLabelText("Minimum per profile"), { target: { value: "1" } });
    fireEvent.change(within(drawer).getByLabelText("Maximum per profile"), { target: { value: "2" } });
    fireEvent.click(within(drawer).getByRole("button", { name: "Preview" }));

    await waitFor(() => expect(within(drawer).getByRole("alert")).toHaveTextContent("out of scope: provider_allocation_preview"));
    expect(api.postProviderAllocationPreviewApiProvidersAllocationPreviewPost).toHaveBeenCalledWith(
      expect.objectContaining({ body: { provider: "codex", profile_ids: ["standard-high-codex"], bounds: { min: 1, max: 2 }, drain: "graceful" } }),
    );
  });
});
