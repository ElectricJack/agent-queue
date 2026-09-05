import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ProjectRoots from "../ProjectRoots";

const api = vi.hoisted(() => ({
  config: { config: { project_roots: [{ id: "local", label: "Local development", path: "/work/local" }] } },
  schema: { schema: { properties: { project_roots: { items: { properties: {
    id: { title: "Root identifier" }, label: { title: "Display label" }, path: { title: "Directory path" },
  } } } } } },
  update: vi.fn(),
}));

vi.mock("../../../api/hooks", () => ({
  useSystemConfig: () => ({ data: api.config, isLoading: false, error: null }),
  useSystemConfigSchema: () => ({ data: api.schema, isLoading: false }),
  useUpdateSystemConfig: () => ({ mutateAsync: api.update, isPending: false }),
}));

function renderRoots() {
  return render(<ProjectRoots />);
}

function rootInputs(index: number) {
  return {
    id: document.getElementById(`project-root-${index}-id`) as HTMLInputElement,
    label: document.getElementById(`project-root-${index}-label`) as HTMLInputElement,
    path: document.getElementById(`project-root-${index}-path`) as HTMLInputElement,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.config = { config: { project_roots: [{ id: "local", label: "Local development", path: "/work/local" }] } };
  api.update.mockResolvedValue({ validation_errors: [], requires_restart: false });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Project roots settings", () => {
  it("adds a root using the fields defined by the config schema and saves the section", async () => {
    renderRoots();
    await waitFor(() => expect(screen.getByLabelText("Root identifier")).toHaveValue("local"));
    fireEvent.click(screen.getByRole("button", { name: "Add project root" }));
    await waitFor(() => expect(rootInputs(1).id).toBeInTheDocument());
    const added = rootInputs(1);
    fireEvent.change(added.id, { target: { value: "scratch" } });
    fireEvent.change(added.label, { target: { value: "Scratch work" } });
    fireEvent.change(added.path, { target: { value: "/work/scratch" } });
    fireEvent.click(screen.getByRole("button", { name: "Save project roots" }));

    await waitFor(() => expect(api.update).toHaveBeenCalledWith({
      section: "project_roots",
      data: [
        { id: "local", label: "Local development", path: "/work/local" },
        { id: "scratch", label: "Scratch work", path: "/work/scratch" },
      ],
    }));
    expect(screen.getByRole("status")).toHaveTextContent("Project roots saved.");
  });

  it("rejects duplicate root IDs before sending an invalid configuration", async () => {
    renderRoots();
    await waitFor(() => expect(screen.getByLabelText("Root identifier")).toHaveValue("local"));
    fireEvent.click(screen.getByRole("button", { name: "Add project root" }));
    await waitFor(() => expect(rootInputs(1).id).toBeInTheDocument());
    fireEvent.change(rootInputs(1).id, { target: { value: "local" } });
    fireEvent.change(rootInputs(1).label, { target: { value: "Another local" } });
    fireEvent.change(rootInputs(1).path, { target: { value: "/work/another" } });
    fireEvent.click(screen.getByRole("button", { name: "Save project roots" }));

    expect(screen.getByText("Each root ID must be unique.")).toBeInTheDocument();
    expect(api.update).not.toHaveBeenCalled();
  });

  it("warns before removing a root and only removes it after confirmation", () => {
    renderRoots();
    fireEvent.click(screen.getByRole("button", { name: "Remove local" }));
    const dialog = screen.getByRole("dialog", { name: "Remove project root?" });
    expect(within(dialog).getByText(/Existing projects beneath this root are unaffected/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Remove root" }));

    expect(screen.queryByRole("button", { name: "Remove local" })).not.toBeInTheDocument();
  });

  it("keeps the draft and displays server validation errors inline", async () => {
    api.update.mockResolvedValueOnce({ validation_errors: ["project_roots[0].path: directory does not exist"], requires_restart: false });
    renderRoots();
    fireEvent.click(screen.getByRole("button", { name: "Save project roots" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("directory does not exist");
    expect(rootInputs(0).path).toHaveAccessibleDescription("directory does not exist");
    expect(rootInputs(0).path).toHaveValue("/work/local");
  });

  it("shows capabilities when the optional root endpoint is available", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ roots: [
      { id: "local", label: "Local development", path: "/work/local", readable: true, writable: false },
    ] }) }));
    renderRoots();

    expect(await screen.findByText("Readable: yes")).toBeInTheDocument();
    expect(screen.getByText("Writable: no")).toBeInTheDocument();
  });
});
