import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import SystemConfig from "../Config";

const PLACEHOLDER = "__aq_redacted__";

const api = vi.hoisted(() => ({
  config: {} as Record<string, unknown>,
  update: vi.fn(),
  reload: vi.fn(),
}));

vi.mock("../../../api/hooks", () => ({
  useSystemConfig: () => ({ data: api.config, isLoading: false, error: null }),
  useSystemConfigSchema: () => ({ data: { schema: { properties: {} } }, isLoading: false }),
  useUpdateSystemConfig: () => ({ mutateAsync: api.update, isPending: false }),
  useReloadSystemConfig: () => ({ mutateAsync: api.reload, isPending: false }),
}));

beforeEach(() => {
  vi.clearAllMocks();
  api.config = {
    path: "/tmp/config.yaml",
    config: {
      database: { url: `postgresql://aq:${PLACEHOLDER}@db.internal:5432/aq` },
      scheduling: { rolling_window_hours: 12 },
    },
    hot_reloadable: ["scheduling"],
    restart_required: ["database"],
    unclassified: [],
    env_var_references: [],
    redacted: ["database.url"],
    secret_placeholder: PLACEHOLDER,
  };
  api.update.mockResolvedValue({ validation_errors: [], requires_restart: true });
});

afterEach(() => cleanup());

describe("System config redaction", () => {
  it("names the redacted paths and explains that saving keeps the stored value", () => {
    render(<SystemConfig />);
    expect(screen.getByText("Redacted credentials")).toBeTruthy();
    expect(screen.getByText("database.url")).toBeTruthy();
    expect(screen.getByText(/keeps the stored value/)).toBeTruthy();
  });

  it("sends the placeholder back verbatim so the daemon restores the credential", async () => {
    render(<SystemConfig />);
    const textarea = document.querySelector("textarea") as HTMLTextAreaElement;
    expect(textarea.value).toContain(PLACEHOLDER);

    fireEvent.change(textarea, {
      target: {
        value: JSON.stringify({
          url: `postgresql://aq:${PLACEHOLDER}@db.internal:5432/other`,
        }),
      },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(api.update).toHaveBeenCalledTimes(1));
    expect(api.update.mock.calls[0]![0]).toEqual({
      section: "database",
      data: { url: `postgresql://aq:${PLACEHOLDER}@db.internal:5432/other` },
    });
  });

  it("does not show the panel for a section with nothing redacted", () => {
    render(<SystemConfig />);
    fireEvent.click(screen.getByText("scheduling"));
    expect(screen.queryByText("Redacted credentials")).toBeNull();
  });
});
