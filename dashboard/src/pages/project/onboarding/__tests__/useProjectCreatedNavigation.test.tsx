import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, useLocation } from "react-router-dom";
import { useProjectCreatedNavigation } from "../useProjectCreatedNavigation";
import type { OnboardingResult } from "../state";

afterEach(cleanup);

let handler: ((result: OnboardingResult) => void) | undefined;

function Probe({ onBefore }: { onBefore?: () => void }) {
  handler = useProjectCreatedNavigation(onBefore);
  const location = useLocation();
  return <output aria-label="Current location">{location.pathname}</output>;
}

describe("useProjectCreatedNavigation", () => {
  it("invalidates project queries, runs the callback, and opens the new project's overview", () => {
    const client = new QueryClient();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    const onBefore = vi.fn();
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/command-center"]}>
          <Probe onBefore={onBefore} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    act(() => handler!({ project_id: "my project" }));
    expect(onBefore).toHaveBeenCalledTimes(1);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["projects"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["workspaces"] });
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/projects/my%20project/overview");
  });

  it("still navigates when no QueryClient is provided", () => {
    render(
      <MemoryRouter initialEntries={["/command-center"]}>
        <Probe />
      </MemoryRouter>,
    );
    act(() => handler!({ project_id: "p2" }));
    expect(screen.getByLabelText("Current location")).toHaveTextContent("/projects/p2/overview");
  });
});
