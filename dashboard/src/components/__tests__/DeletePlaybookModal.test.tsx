import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import DeletePlaybookModal from "../DeletePlaybookModal";

interface Activation {
  playbook_id: string;
  scope: string;
  scope_identifier?: string | null;
  enabled?: boolean;
  active_artifact_sha256?: string | null;
  running_count?: number;
  pending_event_count?: number;
}

const mock = vi.hoisted(() => ({
  del: vi.fn(),
  isPending: false,
  isLoading: false,
  activations: [] as Activation[],
}));

vi.mock("../../api/hooks", () => ({
  useDeletePlaybook: () => ({ mutateAsync: mock.del, isPending: mock.isPending }),
  usePlaybookActivationHealth: () => ({
    data: { activations: mock.activations },
    isLoading: mock.isLoading,
  }),
}));

const READY: Activation = {
  playbook_id: "audit",
  scope: "project",
  scope_identifier: "alpha",
  enabled: false,
  active_artifact_sha256: "a".repeat(64),
};

function show(props: Partial<Parameters<typeof DeletePlaybookModal>[0]> = {}) {
  const onClose = vi.fn();
  const onDeleted = vi.fn();
  render(
    <DeletePlaybookModal
      open
      onClose={onClose}
      onDeleted={onDeleted}
      playbookId="audit"
      scope="project"
      scopeIdentifier="alpha"
      {...props}
    />,
  );
  return { onClose, onDeleted };
}

beforeEach(() => {
  mock.del.mockReset();
  mock.del.mockResolvedValue({ success: true, deleted: true });
  mock.isPending = false;
  mock.isLoading = false;
  mock.activations = [READY];
});
afterEach(() => vi.clearAllMocks());

describe("DeletePlaybookModal", () => {
  it("names the playbook and deletes the exact scoped artifact", async () => {
    const { onClose, onDeleted } = show();

    expect(screen.getByText("audit")).toBeInTheDocument();
    expect(screen.getByText(/project:alpha/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Delete playbook" }));

    expect(mock.del).toHaveBeenCalledWith({
      playbook_id: "audit",
      scope: "project",
      scope_identifier: "alpha",
      artifact_sha256: "a".repeat(64),
    });
    expect(onClose).toHaveBeenCalledOnce();
    expect(onDeleted).toHaveBeenCalledOnce();
  });

  it("cancels without deleting anything", async () => {
    const { onClose, onDeleted } = show();

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(mock.del).not.toHaveBeenCalled();
    expect(onClose).toHaveBeenCalledOnce();
    expect(onDeleted).not.toHaveBeenCalled();
  });

  it("refuses to offer a delete while the playbook is enabled", async () => {
    mock.activations = [{ ...READY, enabled: true }];
    show();

    expect(screen.getByRole("button", { name: "Delete playbook" })).toBeDisabled();
    expect(screen.getByText(/Pause its triggers first/i)).toBeInTheDocument();
  });

  it("refuses to guess a scope when the playbook is installed in several", () => {
    mock.activations = [READY, { ...READY, scope: "system", scope_identifier: "" }];
    show({ scope: undefined, scopeIdentifier: undefined });

    expect(screen.getByRole("button", { name: "Delete playbook" })).toBeDisabled();
    expect(screen.getByText(/installed in more than one scope/i)).toBeInTheDocument();
  });

  it("says so when there is no installed entry left to delete", () => {
    mock.activations = [];
    show();

    expect(screen.getByRole("button", { name: "Delete playbook" })).toBeDisabled();
    expect(screen.getByText(/No installed entry for this playbook/i)).toBeInTheDocument();
  });

  it("warns about unfinished work the daemon will refuse to delete over", () => {
    mock.activations = [{ ...READY, running_count: 1, pending_event_count: 1 }];
    show();

    expect(screen.getByRole("button", { name: "Delete playbook" })).toBeEnabled();
    expect(screen.getByText(/2 unfinished run or pending event/)).toBeInTheDocument();
  });

  it("shows a refusal in place and leaves the playbook alone", async () => {
    mock.del.mockRejectedValue(new Error("API 422: playbook still owns unfinished work; deletion refused"));
    const { onClose, onDeleted } = show();

    await userEvent.click(screen.getByRole("button", { name: "Delete playbook" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("deletion refused");
    expect(onClose).not.toHaveBeenCalled();
    expect(onDeleted).not.toHaveBeenCalled();
  });
});
