import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import ActivationPanel from "../ActivationPanel";
import ArtifactDiffPanel from "../ArtifactDiffPanel";
import PendingEventsPanel from "../PendingEventsPanel";
import RunOverlayPanel from "../RunOverlayPanel";

const sha = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const artifact = { playbook_id: "review", artifact_sha256: sha, schema_generation: 2, contract_fingerprint: "contracts", source_digest: "source", compiler_build: "test" };
const activation = { playbook_id: "review", scope: "system", enabled: true, active_artifact_sha256: sha, health: "stale_contract", reasons: [{ code: "command_contract_changed", message: "gate_create changed" }], pending_event_count: 1, running_count: 0 };

describe("Package 5 review panels", () => {
  it("separates executable changes and requires acknowledgement before activation", async () => {
    const activate = vi.fn();
    const user = userEvent.setup();
    render(<><ArtifactDiffPanel diff={{ base: artifact, target: artifact, executable_change: true, semantic_change_count: 1, presentation_change_count: 1, steps: [{ step_id: "gate", change: "modified", field_changes: [{ path: "/command", executable: true }, { path: "/title", executable: false }] }] }} /><ActivationPanel artifact={artifact} activation={activation} executableChange onActivate={activate} /></>);
    expect(screen.getByText("Executable changes")).toBeInTheDocument();
    expect(screen.getByText("Presentation-only changes")).toBeInTheDocument();
    const button = screen.getByRole("button", { name: "Activate displayed artifact" });
    expect(button).toBeDisabled();
    await user.click(screen.getByRole("checkbox", { name: "I reviewed the executable diff" }));
    await user.click(button);
    expect(activate).toHaveBeenCalledWith(sha);
  });

  it("requires a fresh acknowledgement when the displayed artifact changes", async () => {
    const activate = vi.fn();
    const user = userEvent.setup();
    const { rerender } = render(
      <ActivationPanel
        artifact={artifact}
        activation={activation}
        executableChange
        onActivate={activate}
      />,
    );
    await user.click(screen.getByRole("checkbox", { name: "I reviewed the executable diff" }));
    expect(screen.getByRole("button", { name: "Activate displayed artifact" })).toBeEnabled();

    const nextArtifact = {
      ...artifact,
      artifact_sha256: "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    };
    rerender(
      <ActivationPanel
        artifact={nextArtifact}
        activation={activation}
        executableChange
        onActivate={activate}
      />,
    );

    expect(screen.getByRole("checkbox", { name: "I reviewed the executable diff" })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Activate displayed artifact" })).toBeDisabled();
  });

  it("lists the inactive candidates and reports the chosen hash", async () => {
    const select = vi.fn();
    const user = userEvent.setup();
    const candidate = "sha256:" + "6".repeat(64);
    render(
      <ActivationPanel
        artifact={artifact}
        activation={activation}
        executableChange={false}
        onActivate={vi.fn()}
        artifacts={[
          { artifact: { ...artifact, artifact_sha256: candidate, version: 6 }, is_active: false },
          { artifact: { ...artifact, version: 5 }, is_active: true },
        ]}
        selectedSha={sha}
        onSelect={select}
      />,
    );

    const chooser = screen.getByLabelText("Artifact under review");
    expect(chooser).toHaveValue(sha);
    expect(screen.getByRole("option", { name: /v5 .* \(active\)/ })).toBeInTheDocument();
    await user.selectOptions(chooser, candidate);
    expect(select).toHaveBeenCalledWith(candidate);
  });

  it("refuses activation while the diff reports a blocker, and names it", async () => {
    const activate = vi.fn();
    const user = userEvent.setup();
    render(
      <ActivationPanel
        artifact={artifact}
        activation={activation}
        executableChange
        activationBlocked
        activationBlockers={["Command contract changed for 'gate_create'"]}
        onActivate={activate}
      />,
    );

    expect(screen.getByText("Command contract changed for 'gate_create'")).toBeInTheDocument();
    const button = screen.getByRole("button", { name: "Activate displayed artifact" });
    expect(button).toBeDisabled();
    // Acknowledging the executable diff does not unblock a stale contract.
    await user.click(screen.getByRole("checkbox", { name: "I reviewed the executable diff" }));
    expect(button).toBeDisabled();
    expect(activate).not.toHaveBeenCalled();
  });

  it("has no chooser at all when the artifact list has not loaded", () => {
    render(
      <ActivationPanel
        artifact={artifact}
        activation={activation}
        executableChange={false}
        onActivate={vi.fn()}
      />,
    );
    expect(screen.queryByLabelText("Artifact under review")).toBeNull();
  });

  it("offers dispatch and discard for every pending event", async () => {
    const action = vi.fn();
    const user = userEvent.setup();
    render(<PendingEventsPanel events={[{ pending_event_id: "event-1", event_type: "task.completed", received_at: 0, reason: "stale_contract", attempts: 2 }]} onAction={action} />);
    expect(screen.getByText("stale contract")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Dispatch event event-1" }));
    await user.click(screen.getByRole("button", { name: "Discard event event-1" }));
    expect(action).toHaveBeenNthCalledWith(1, "dispatch", ["event-1"]);
    expect(action).toHaveBeenNthCalledWith(2, "discard", ["event-1"]);
  });

  it("pins an older artifact and keeps each loop iteration receipt selectable", async () => {
    const user = userEvent.setup();
    render(<RunOverlayPanel overlay={{ run_id: "run-1", artifact, artifact_is_active: false, lifecycle: "completed", rule_id: "review", nodes: [{ step_id: "loop", state: "completed", visit_count: 2, iterations: [{ index: 0, item_display: "one", receipt_ids: ["r1"] }, { index: 1, item_display: "two", receipt_ids: ["r2"] }] }], receipts: [{ receipt_id: "r1", step_id: "loop", rule_id: "review", step_kind: "foreach", outcome: "success", started_at: 0 }, { receipt_id: "r2", step_id: "loop", rule_id: "review", step_kind: "foreach", outcome: "failed", started_at: 1 }], edges: [] }} />);
    expect(screen.getByText(/older artifact/i)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /iteration 1: two/i }));
    expect(screen.getByText("Outcome: failed")).toBeInTheDocument();
  });
});
