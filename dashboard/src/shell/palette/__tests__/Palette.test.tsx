import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, test, vi, beforeEach } from "vitest";
import { z } from "zod";
import { Palette } from "../Palette";
import { PaletteStateProvider, usePaletteState } from "../paletteState";
import { ActionRegistryProvider } from "../registerActions";
import { ShortcutsProvider } from "../../hotkeys/useShortcuts";
import { ShellPaneProvider, useShellPaneStore } from "../../../panes/store";
import type { PaneEntry } from "../../../panes/registry";

const data = vi.hoisted(() => ({
  projects: [] as { id: string; name?: string }[],
  tasks: [] as { id: string; title?: string; project_id: string }[],
}));

vi.mock("../../../api/hooks", () => ({
  useProjects: () => ({ data: data.projects }),
  useActiveTasksAllProjects: () => ({ data: data.tasks }),
}));

vi.mock("../../../panes/registry", () => ({ PANE_REGISTRY: {} }));

const paneRegistry: Record<string, PaneEntry> = {
  test: {
    manifest: {
      id: "test", name: "Test", description: "test",
      icon: (() => null) as never, args_schema: z.object({}),
    },
    Component: () => null,
  },
};

function OpenPalette() {
  const { setOpen } = usePaletteState();
  return <button type="button" onClick={() => setOpen(true)}>Open palette</button>;
}

function PaneState() {
  const { state, open } = useShellPaneStore();
  return (
    <>
      <button type="button" onClick={() => open("test", {})}>Open test pane</button>
      <output data-testid="pane-state">{state.kind}</output>
    </>
  );
}

function RouteState() {
  const location = useLocation();
  return <output data-testid="route-state">{JSON.stringify({
    path: location.pathname + location.search,
    from: (location.state as { from?: string } | null)?.from,
  })}</output>;
}

function harness(initialEntry = "/projects/current/tasks?q=work") {
  const qc = new QueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <ShellPaneProvider registryOverride={paneRegistry}>
          <PaletteStateProvider>
            <ShortcutsProvider>
              <ActionRegistryProvider>
                <OpenPalette />
                <PaneState />
                <RouteState />
                <Palette />
              </ActionRegistryProvider>
            </ShortcutsProvider>
          </PaletteStateProvider>
        </ShellPaneProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function openWithPrefix(prefix: "#" | "@") {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Open test pane" }));
  await user.click(screen.getByRole("button", { name: "Open palette" }));
  await user.type(screen.getByPlaceholderText(/type a command/i), prefix);
  return user;
}

beforeEach(() => {
  data.projects = [];
  data.tasks = [];
});

describe("Palette", () => {
  test("$mod-K opens palette and > prefix scopes to actions", async () => {
    const userAgent = Object.getOwnPropertyDescriptor(navigator, "userAgent");
    Object.defineProperty(navigator, "userAgent", {
      value: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
      configurable: true,
    });
    try {
      harness();
      await userEvent.keyboard("{Meta>}k{/Meta}");
      expect(await screen.findByRole("dialog")).toBeInTheDocument();
      await userEvent.keyboard(">open");
      expect(screen.getByText("No results.")).toBeInTheDocument();
    } finally {
      if (userAgent) Object.defineProperty(navigator, "userAgent", userAgent);
    }
  });

  test.each(["/projects/current/tasks?q=work", "/settings/profiles"])("task selection retains %s and closes the active pane", async (origin) => {
    data.tasks = [{ id: "task / 1", title: "Build release", project_id: "current" }];
    harness(origin);
    const user = await openWithPrefix("#");
    await user.click(screen.getByText("Build release"));

    expect(screen.getByTestId("route-state")).toHaveTextContent(JSON.stringify({
      path: "/tasks/task%20%2F%201",
      from: origin,
    }));
    expect(screen.getByTestId("pane-state")).toHaveTextContent("closed");
  });

  test("project selection retains the workspace tab and filters without closing its pane", async () => {
    data.projects = [{ id: "target / project", name: "Target project" }];
    harness();
    const user = await openWithPrefix("@");
    await user.click(screen.getByText("Target project"));

    expect(screen.getByTestId("route-state")).toHaveTextContent(JSON.stringify({
      path: "/projects/target%20%2F%20project/tasks?q=work",
    }));
    expect(screen.getByTestId("pane-state")).toHaveTextContent("open");
  });
});
