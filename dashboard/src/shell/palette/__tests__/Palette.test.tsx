import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, test, vi } from "vitest";
import { Palette } from "../Palette";
import { ActionRegistryProvider, useRegisterAction } from "../registerActions";
import { ShortcutsProvider } from "../../hotkeys/useShortcuts";

vi.mock("../../../api/hooks", () => ({
  useProjects: () => ({ data: [] }),
  useActiveTasksAllProjects: () => ({ data: [] }),
}));

function Registrar() {
  useRegisterAction({
    id: "open-diff",
    label: "Open diff for current task",
    run: () => {},
    section: "Panes",
  });
  return null;
}

function harness() {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ShortcutsProvider>
          <ActionRegistryProvider>
            <Registrar />
            <Palette />
          </ActionRegistryProvider>
        </ShortcutsProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("Palette", () => {
  test("$mod-K opens palette and > prefix scopes to actions", async () => {
    Object.defineProperty(navigator, "userAgent", {
      value: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
      configurable: true,
    });
    harness();
    await userEvent.keyboard("{Meta>}k{/Meta}");
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    await userEvent.keyboard(">open");
    expect(screen.getByText("Open diff for current task")).toBeInTheDocument();
  });
});
