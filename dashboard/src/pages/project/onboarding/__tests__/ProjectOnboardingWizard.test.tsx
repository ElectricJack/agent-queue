import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { useEffect, useRef, useState } from "react";
import { axe, toHaveNoViolations } from "jest-axe";

expect.extend(toHaveNoViolations);
import ProjectOnboardingWizard, {
  FieldError,
  createStepRegistry,
  useFieldErrorProps,
  useWizard,
  type ProjectRootsSource,
  type SubmitProject,
} from "..";

afterEach(cleanup);

const READY: ProjectRootsSource = {
  status: "ready",
  roots: [{ id: "dev", label: "Development", displayPath: "~/dev", readable: true, writable: true }],
};
const EMPTY: ProjectRootsSource = { status: "ready", roots: [] };

type HarnessProps = Partial<Parameters<typeof ProjectOnboardingWizard>[0]> & { initiallyOpen?: boolean };

function Harness({ initiallyOpen = true, ...props }: HarnessProps) {
  const ref = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(initiallyOpen);
  return (
    <MemoryRouter>
      <button type="button" ref={ref} onClick={() => setOpen(true)}>Add project</button>
      <ProjectOnboardingWizard
        open={open}
        onClose={() => setOpen(false)}
        returnFocusRef={ref}
        roots={READY}
        {...props}
      />
    </MemoryRouter>
  );
}

function dialog() {
  return screen.getByRole("dialog", { name: "Add project" });
}
function liveRegion() {
  return screen.getByRole("status");
}

async function chooseSource(user: ReturnType<typeof userEvent.setup>, label: RegExp) {
  await user.click(within(dialog()).getByRole("radio", { name: label }));
}
async function goToReview(user: ReturnType<typeof userEvent.setup>) {
  for (let i = 0; i < 4; i++) await user.click(screen.getByRole("button", { name: "Next" }));
}

describe("ProjectOnboardingWizard shell", () => {
  it("is a labelled modal dialog that announces the current step", () => {
    render(<Harness />);
    const dlg = dialog();
    expect(dlg).toHaveAttribute("aria-modal", "true");
    expect(liveRegion()).toHaveAttribute("aria-live", "polite");
    expect(liveRegion()).toHaveTextContent("Step 1 of 5: Choose source");
    expect(within(dlg).getByRole("list", { name: "Steps" })).toBeInTheDocument();
  });

  it("offers the three source choices and unlocks Next once one is chosen", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const group = within(dialog()).getByRole("radiogroup", { name: "Source" });
    expect(within(group).getAllByRole("radio").map((r) => r.getAttribute("aria-label") ?? r.textContent)).toHaveLength(3);
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    await chooseSource(user, /Existing local repository/);
    expect(screen.getByRole("button", { name: "Next" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(liveRegion()).toHaveTextContent("Step 2 of 5: Choose repository");
    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(within(dialog()).getByRole("radio", { name: /Existing local repository/ })).toBeChecked();
  });

  it("marks the current step for assistive tech with text, not colour", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await chooseSource(user, /New repository/);
    await user.click(screen.getByRole("button", { name: "Next" }));
    const steps = within(within(dialog()).getByRole("list", { name: "Steps" })).getAllByRole("listitem");
    expect(steps[0]).toHaveTextContent(/completed/i);
    expect(steps[1]).toHaveAttribute("aria-current", "step");
    expect(steps[1]).toHaveTextContent(/current/i);
  });

  it("moves focus into the dialog on open and traps Tab inside it", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const dlg = dialog();
    expect(dlg.contains(document.activeElement)).toBe(true);
    await chooseSource(user, /New repository/);
    const last = within(dlg).getByRole("button", { name: "Next" });
    expect(last).toBeEnabled();
    last.focus();
    await user.tab();
    expect(dlg.contains(document.activeElement)).toBe(true);
    expect(document.activeElement).not.toBe(last);
    const first = document.activeElement as HTMLElement;
    await user.tab({ shift: true });
    expect(document.activeElement).toBe(last);
    expect(first).not.toBe(last);
  });

  it("closes on Escape and returns focus to the Add project button", async () => {
    const user = userEvent.setup();
    render(<Harness initiallyOpen={false} />);
    const opener = screen.getByRole("button", { name: "Add project" });
    await user.click(opener);
    expect(dialog()).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("performs no mutation when closed before submission", async () => {
    const user = userEvent.setup();
    const submit = vi.fn();
    render(<Harness submit={submit} />);
    await chooseSource(user, /Clone from GitHub/);
    await goToReview(user);
    await user.click(within(dialog()).getByRole("button", { name: "Close dialog" }));
    expect(submit).not.toHaveBeenCalled();
  });

  it("shows the no-roots empty state with a Settings link instead of a path input", () => {
    render(<Harness roots={EMPTY} />);
    const dlg = dialog();
    expect(within(dlg).getByText("No project roots configured", { selector: "p" })).toBeInTheDocument();
    expect(within(dlg).getByRole("link", { name: /Settings/ })).toHaveAttribute("href", "/settings/config");
    expect(within(dlg).queryByRole("textbox")).not.toBeInTheDocument();
    expect(within(dlg).queryByRole("radiogroup")).not.toBeInTheDocument();
    expect(liveRegion()).toHaveTextContent("No project roots configured");
  });

  it("labels the review action by source and reports submission phases", async () => {
    const user = userEvent.setup();
    let release: (() => void) | undefined;
    const submit = vi.fn<SubmitProject>(async (_request, { onPhase }) => {
      onPhase("Cloning repository");
      await new Promise<void>((resolve) => { release = resolve; });
      return { project_id: "widgets" };
    });
    const onSuccess = vi.fn();
    render(<Harness submit={submit} onSuccess={onSuccess} />);
    await chooseSource(user, /Clone from GitHub/);
    await goToReview(user);
    expect(liveRegion()).toHaveTextContent("Step 5 of 5: Review and create");
    await user.click(screen.getByRole("button", { name: "Clone and add project" }));
    expect(submit).toHaveBeenCalledWith(
      expect.objectContaining({ mode: "github_clone", identity: expect.objectContaining({ defaultBranch: "main" }) }),
      expect.objectContaining({ onPhase: expect.any(Function) }),
    );
    await waitFor(() => expect(liveRegion()).toHaveTextContent("Cloning repository"));
    expect(within(dialog()).getByText(/Cloning repository/, { selector: "p span" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Back" })).toBeDisabled();
    release!();
    await waitFor(() => expect(onSuccess).toHaveBeenCalledWith({ project_id: "widgets" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("focuses an error summary after a failed submission and keeps entered values", async () => {
    const user = userEvent.setup();
    const submit = vi.fn<SubmitProject>(async () => {
      throw { message: "Could not create the project", code: "project_id_conflict", fieldErrors: { projectId: "Already in use" } };
    });
    render(<Harness submit={submit} />);
    await chooseSource(user, /New repository/);
    await goToReview(user);
    await user.click(screen.getByRole("button", { name: "Create project" }));
    const summary = await screen.findByRole("alert");
    expect(summary).toHaveFocus();
    expect(summary).toHaveTextContent("Could not create the project");
    expect(summary).toHaveTextContent("Already in use");
    expect(liveRegion()).toHaveTextContent(/failed/i);
    expect(screen.getByRole("button", { name: "Create project" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Back" }));
    await user.click(screen.getByRole("button", { name: "Back" }));
    await user.click(screen.getByRole("button", { name: "Back" }));
    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(within(dialog()).getByRole("radio", { name: /New repository/ })).toBeChecked();
  });

  it("derives editable identity values and surfaces an obvious loaded-project collision", async () => {
    function RepositoryStep() {
      const { dispatch } = useWizard();
      useEffect(() => {
        dispatch({ type: "update_source", mode: "init", patch: { directoryName: "My Widgets" } });
      }, [dispatch]);
      return <p>Repository selected</p>;
    }
    const user = userEvent.setup();
    render(<Harness projectIds={["my-widgets"]} steps={createStepRegistry({ repository: RepositoryStep })} />);
    await chooseSource(user, /New repository/);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByRole("textbox", { name: "Display name" })).toHaveValue("My Widgets");
    const projectId = screen.getByRole("textbox", { name: "Project ID" });
    expect(projectId).toHaveValue("my-widgets");
    expect(screen.getByText(/already in use/i)).toBeInTheDocument();
    await user.clear(projectId);
    await user.type(projectId, "available-widgets");
    expect(screen.queryByText(/already in use/i)).not.toBeInTheDocument();
  });

  it("lists every persistent init action and warns when GitHub creation cannot push", async () => {
    function RepositoryStep() {
      const { dispatch } = useWizard();
      useEffect(() => {
        dispatch({ type: "update_source", mode: "init", patch: { directoryName: "widgets", createReadme: false, createGithub: true, githubOwner: "acme", githubRepo: "widgets" } });
      }, [dispatch]);
      return <p>Repository selected</p>;
    }
    const user = userEvent.setup();
    render(<Harness steps={createStepRegistry({ repository: RepositoryStep })} />);
    await chooseSource(user, /New repository/);
    await goToReview(user);
    expect(screen.getByText(/Create a new Git repository/)).toBeInTheDocument();
    expect(screen.getByText(/Register project/)).toBeInTheDocument();
    expect(screen.getByText(/GitHub creation/)).toBeInTheDocument();
    expect(screen.getByText(/without pushing a branch/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create project" })).toBeEnabled();
  });

  it("lets plugged-in steps associate field errors with their inputs", async () => {
    function IdentityStep() {
      const { state, dispatch } = useWizard();
      const props = useFieldErrorProps("projectId");
      return (
        <div>
          <label htmlFor="project-id">Project ID</label>
          <input
            id="project-id"
            value={state.identity.projectId}
            onChange={(e) => dispatch({ type: "update_identity", patch: { projectId: e.target.value } })}
            {...props}
          />
          <FieldError name="projectId" />
        </div>
      );
    }
    const user = userEvent.setup();
    const submit = vi.fn<SubmitProject>(async () => {
      throw { message: "Fix the highlighted fields", fieldErrors: { projectId: "Already in use" } };
    });
    render(<Harness submit={submit} steps={createStepRegistry({ identity: IdentityStep })} />);
    await chooseSource(user, /Existing local repository/);
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    const input = screen.getByRole("textbox", { name: "Project ID" });
    expect(input).not.toHaveAttribute("aria-invalid", "true");
    await user.type(input, "widgets");
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Next" }));
    await user.click(screen.getByRole("button", { name: "Link project" }));
    await screen.findByRole("alert");
    await user.click(within(screen.getByRole("alert")).getByRole("button", { name: /Project ID/ }));
    const again = screen.getByRole("textbox", { name: "Project ID" });
    expect(again).toHaveValue("widgets");
    expect(again).toHaveAttribute("aria-invalid", "true");
    const describedBy = again.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)).toHaveTextContent("Already in use");
    expect(again).toHaveFocus();
  });

  it("has no axe violations on the first step or the empty state", async () => {
    const { container, unmount } = render(<Harness />);
    expect(await axe(container)).toHaveNoViolations();
    unmount();
    const empty = render(<Harness roots={EMPTY} />);
    expect(await axe(empty.container)).toHaveNoViolations();
  });
});
