import { describe, expect, it, vi } from "vitest";
import { refetchLayout, registerLayoutRefetch } from "../liveRegistry";

describe("liveRegistry", () => {
  it("calls every refetch registered for a project and nothing from other projects", () => {
    const one = vi.fn(), two = vi.fn(), other = vi.fn();
    registerLayoutRefetch("p1", one);
    registerLayoutRefetch("p1", two);
    registerLayoutRefetch("p2", other);
    refetchLayout("p1");
    expect(one).toHaveBeenCalledTimes(1);
    expect(two).toHaveBeenCalledTimes(1);
    expect(other).not.toHaveBeenCalled();
  });

  it("stops calling a refetch once its registration is disposed", () => {
    const fn = vi.fn();
    const dispose = registerLayoutRefetch("p3", fn);
    dispose();
    refetchLayout("p3");
    expect(fn).not.toHaveBeenCalled();
  });
});
