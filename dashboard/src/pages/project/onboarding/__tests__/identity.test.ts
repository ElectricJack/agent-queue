import { describe, expect, it } from "vitest";
import { deriveProjectIdentity, projectIdError } from "../identity";
import type { SourceState } from "../state";

describe("project identity helpers", () => {
  it("derives a readable name and URL-safe id from the selected repository", () => {
    const source: SourceState = {
      mode: "link",
      rootId: "development",
      relativePath: "clients/My Widgets!",
    };

    expect(deriveProjectIdentity(source)).toEqual({
      projectName: "My Widgets!",
      projectId: "my-widgets",
    });
  });

  it("uses the new directory name for a repository being initialized", () => {
    const source: SourceState = {
      mode: "init",
      rootId: "development",
      directoryName: "my_new.widgets",
      createReadme: true,
      createGithub: false,
      githubOwner: null,
      githubRepo: "",
      githubVisibility: "private",
    };

    expect(deriveProjectIdentity(source)).toEqual({
      projectName: "my_new.widgets",
      projectId: "my_new.widgets",
    });
  });

  it("reports malformed ids and obvious loaded-project collisions", () => {
    expect(projectIdError("My widgets", ["other-project"])).toMatch(/URL-safe/i);
    expect(projectIdError("widgets", ["widgets"])).toMatch(/already in use/i);
    expect(projectIdError("available-project", ["widgets"])).toBeNull();
  });
});
