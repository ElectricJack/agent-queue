import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import Messaging from "../Messaging";

const api = vi.hoisted(() => ({
  config: {} as { config: Record<string, unknown> },
  update: vi.fn(),
  status: {} as Record<string, unknown>,
  preview: {} as Record<string, unknown>,
  previewRefetch: vi.fn(),
  statusRefetch: vi.fn(),
  escalations: [] as Array<Record<string, unknown>>,
  reply: vi.fn(),
}));

vi.mock("../../../api/hooks", () => ({
  useSystemConfig: () => ({ data: api.config, isLoading: false, error: null }),
  useUpdateSystemConfig: () => ({ mutateAsync: api.update, isPending: false }),
}));

vi.mock("../../../api/messaging", () => ({
  useDigestStatus: () => ({ data: api.status, refetch: api.statusRefetch }),
  useDigestPreview: (enabled: boolean) => ({
    data: enabled ? api.preview : undefined,
    isLoading: false,
    error: null,
    refetch: api.previewRefetch,
  }),
  useEscalations: () => ({ data: { escalations: api.escalations }, isLoading: false, error: null }),
  useEscalation: () => ({ data: undefined }),
  useEscalationReply: () => ({ mutateAsync: api.reply, isPending: false }),
}));

function baseDiscord(overrides: Record<string, unknown> = {}) {
  return {
    bot_token: "${DISCORD_BOT_TOKEN}",
    guild_id: "999999999999999999",
    channel_id: "123456789012345678",
    digest: {
      enabled: true,
      interval_minutes: 60,
      project_ids: [],
      categories: ["work", "vcs", "budget", "system"],
      catchup_hours: 24,
    },
    escalation: {
      enabled: true,
      mention_user_ids: [],
      mention_role_ids: [],
      reminder_minutes: 0,
      supervisor_delivery_timeout_minutes: 15,
    },
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.config = { config: { discord: baseDiscord() } };
  api.update.mockResolvedValue({ validation_errors: [] });
  api.status = {
    destination: "discord:123456789012345678",
    config_generation: 4242,
    next_evaluation_at: 1_788_000_000,
    last_window_end: 1_787_996_400,
    delivery_health: { pending: 0, sending: 0, retry: 1, unknown: 2 },
    open_escalations: 1,
    pending_escalation_deliveries: 3,
    settings_errors: [],
    warnings: [],
  };
  api.preview = { would_send: false, reason: "idle_only", suppression_reason: "idle_only", text: "" };
  api.escalations = [];
});

afterEach(cleanup);

function renderPage() {
  return render(
    <MemoryRouter>
      <Messaging />
    </MemoryRouter>,
  );
}

describe("Messaging settings", () => {
  it("saves independent digest and escalation settings to the discord config section", async () => {
    renderPage();
    fireEvent.change(screen.getByLabelText("Interval (minutes)"), { target: { value: "120" } });
    fireEvent.click(screen.getByLabelText("budget"));
    fireEvent.click(screen.getByLabelText("Enabled"));
    fireEvent.change(screen.getByLabelText("Projects (blank = every project this destination may see)"), {
      target: { value: "agent-queue, other" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save messaging settings" }));

    await waitFor(() => expect(api.update).toHaveBeenCalledTimes(1));
    const payload = api.update.mock.calls[0]![0] as {
      section: string;
      data: {
        bot_token: string;
        digest: Record<string, unknown>;
        escalation: { enabled: boolean };
      };
    };
    expect(payload.section).toBe("discord");
    expect(payload.data.digest).toMatchObject({
      enabled: false,
      interval_minutes: 120,
      categories: ["work", "vcs", "system"],
      project_ids: ["agent-queue", "other"],
    });
    // Escalation enablement is untouched by a digest change.
    expect(payload.data.escalation.enabled).toBe(true);
    // Unrelated Discord settings (credentials, guild) survive the round trip.
    expect(payload.data.bot_token).toBe("${DISCORD_BOT_TOKEN}");
  });

  it("refuses out-of-bounds intervals, bad IDs and an empty category set before saving", async () => {
    renderPage();
    fireEvent.change(screen.getByLabelText("Interval (minutes)"), { target: { value: "5" } });
    fireEvent.change(screen.getByLabelText("Catch-up horizon (hours)"), { target: { value: "200" } });
    fireEvent.change(screen.getByLabelText("Channel ID"), { target: { value: "agent-queue" } });
    fireEvent.change(screen.getByLabelText("Mention user IDs"), { target: { value: "nope" } });
    for (const category of ["work", "vcs", "budget", "system"]) {
      fireEvent.click(screen.getByLabelText(category));
    }

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Digest interval must be between 15 and 1440 minutes.");
    expect(alert).toHaveTextContent("Catch-up horizon must be between 1 and 168 hours.");
    expect(alert).toHaveTextContent("Channel ID must be a Discord ID");
    expect(alert).toHaveTextContent("Mention ID nope is not a Discord ID");
    expect(alert).toHaveTextContent("Select at least one digest category");
    expect(screen.getByRole("button", { name: "Save messaging settings" })).toBeDisabled();
    expect(api.update).not.toHaveBeenCalled();
  });

  it("warns that disabling external escalation keeps core and dashboard handling", async () => {
    api.config = { config: { discord: baseDiscord({
      escalation: { enabled: false, mention_user_ids: [], mention_role_ids: [], reminder_minutes: 0, supervisor_delivery_timeout_minutes: 15 },
    }) } };
    renderPage();
    const alerts = await screen.findAllByRole("alert");
    expect(alerts[0]).toHaveTextContent("External escalation posting is disabled");
    expect(alerts[0]).toHaveTextContent("escalation inbox still works");
  });

  it("shows next evaluation, generation and delivery health needing attention", async () => {
    renderPage();
    expect(screen.getByText("discord:123456789012345678")).toBeInTheDocument();
    expect(screen.getByText("4242")).toBeInTheDocument();
    expect(screen.getByText(new Date(1_788_000_000 * 1000).toLocaleString())).toBeInTheDocument();
    expect(screen.getByText(/Digest deliveries needing attention: retry 1, unknown 2/)).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
  });

  it("previews the digest only when asked, and shows the suppression reason", async () => {
    renderPage();
    expect(screen.queryByText("Digest preview")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Preview digest" }));
    await waitFor(() => expect(screen.getByText("Digest preview")).toBeInTheDocument());
    expect(screen.getByText(/nothing is sent and no delivery cursor moves/)).toBeInTheDocument();
    expect(screen.getByText("idle_only")).toBeInTheDocument();
  });
});
