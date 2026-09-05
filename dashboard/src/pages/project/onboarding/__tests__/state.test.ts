import { describe, expect, it } from "vitest";
import {
  STEP_IDS,
  initialWizardState,
  reviewActionLabel,
  canAdvance,
  wizardReducer,
  type WizardAction,
  type WizardState,
} from "../state";

function run(actions: WizardAction[], start: WizardState = initialWizardState()): WizardState {
  return actions.reduce(wizardReducer, start);
}

describe("onboarding wizard state", () => {
  it("starts on the source step with no source mode chosen", () => {
    const s = initialWizardState();
    expect(STEP_IDS[s.stepIndex]).toBe("source");
    expect(s.source.mode).toBeNull();
    expect(s.submission.status).toBe("idle");
  });

  it("cannot advance from the source step until a mode is chosen", () => {
    const s = initialWizardState();
    expect(canAdvance(s)).toBe(false);
    expect(run([{ type: "go_next" }], s).stepIndex).toBe(0);
    const chosen = run([{ type: "set_source_mode", mode: "link" }], s);
    expect(canAdvance(chosen)).toBe(true);
    expect(run([{ type: "go_next" }], chosen).stepIndex).toBe(1);
  });

  it("back navigation preserves entered values", () => {
    const s = run([
      { type: "set_source_mode", mode: "init" },
      { type: "update_source", mode: "init", patch: { rootId: "dev", directoryName: "widgets" } },
      { type: "go_next" },
      { type: "go_next" },
      { type: "update_identity", patch: { projectName: "Widgets", projectId: "widgets" } },
      { type: "go_back" },
      { type: "go_back" },
    ]);
    expect(STEP_IDS[s.stepIndex]).toBe("source");
    expect(s.source).toMatchObject({ mode: "init", rootId: "dev", directoryName: "widgets" });
    expect(s.identity).toMatchObject({ projectName: "Widgets", projectId: "widgets" });
  });

  it("switching source mode keeps identity and clears source-specific values", () => {
    const linked = run([
      { type: "set_source_mode", mode: "link" },
      { type: "update_source", mode: "link", patch: { rootId: "dev", relativePath: "tools/widgets" } },
      { type: "update_identity", patch: { projectName: "Widgets", projectId: "widgets", defaultBranch: "trunk" } },
    ]);
    const switched = run([{ type: "set_source_mode", mode: "github_clone" }], linked);
    expect(switched.identity).toEqual(linked.identity);
    expect(switched.source).toEqual({
      mode: "github_clone",
      rootId: null,
      githubRepository: null,
      githubUrl: "",
    });
    const back = run([{ type: "set_source_mode", mode: "link" }], switched);
    expect(back.source).toEqual({ mode: "link", rootId: null, relativePath: null });
  });

  it("re-selecting the current source mode is a no-op", () => {
    const s = run([
      { type: "set_source_mode", mode: "link" },
      { type: "update_source", mode: "link", patch: { rootId: "dev", relativePath: "a" } },
    ]);
    expect(run([{ type: "set_source_mode", mode: "link" }], s)).toBe(s);
  });

  it("ignores a source patch addressed to a different mode", () => {
    const s = run([{ type: "set_source_mode", mode: "link" }]);
    expect(run([{ type: "update_source", mode: "init", patch: { directoryName: "x" } }], s)).toBe(s);
  });

  it("new repositories default to main, a README commit, and no GitHub repo", () => {
    const s = run([{ type: "set_source_mode", mode: "init" }]);
    expect(s.identity.defaultBranch).toBe("main");
    expect(s.source).toMatchObject({ createReadme: true, createGithub: false, githubVisibility: "private" });
  });

  it("go_to only reaches steps already unlocked", () => {
    const s = run([{ type: "set_source_mode", mode: "link" }, { type: "go_next" }, { type: "go_next" }]);
    expect(s.stepIndex).toBe(2);
    expect(run([{ type: "go_to", step: "source" }], s).stepIndex).toBe(0);
    expect(run([{ type: "go_to", step: "review" }], s).stepIndex).toBe(2);
  });

  it("never advances past the review step", () => {
    const s = run([{ type: "set_source_mode", mode: "link" }, ...Array(10).fill({ type: "go_next" } as WizardAction)]);
    expect(STEP_IDS[s.stepIndex]).toBe("review");
  });

  it("tracks submission phases and keeps values after a failure", () => {
    const ready = run([
      { type: "set_source_mode", mode: "github_clone" },
      { type: "update_source", mode: "github_clone", patch: { githubUrl: "https://github.com/acme/widgets" } },
    ]);
    const submitting = run([{ type: "submit_started" }, { type: "submit_phase", phase: "Cloning repository" }], ready);
    expect(submitting.submission).toEqual({ status: "submitting", phase: "Cloning repository" });
    expect(run([{ type: "go_back" }], submitting).stepIndex).toBe(submitting.stepIndex);
    const failed = run(
      [{ type: "submit_failed", error: { message: "Boom", code: "clone_failed", fieldErrors: { projectId: "Taken" } } }],
      submitting,
    );
    expect(failed.submission).toEqual({
      status: "failed",
      error: { message: "Boom", code: "clone_failed", fieldErrors: { projectId: "Taken" } },
    });
    expect(failed.source).toEqual(ready.source);
    const ok = run([{ type: "submit_succeeded", result: { project_id: "widgets" } }], submitting);
    expect(ok.submission).toEqual({ status: "succeeded", result: { project_id: "widgets" } });
  });

  it("labels the review action by source mode", () => {
    expect(reviewActionLabel("link")).toBe("Link project");
    expect(reviewActionLabel("init")).toBe("Create project");
    expect(reviewActionLabel("github_clone")).toBe("Clone and add project");
    expect(reviewActionLabel(null)).toBe("Create project");
  });
});
