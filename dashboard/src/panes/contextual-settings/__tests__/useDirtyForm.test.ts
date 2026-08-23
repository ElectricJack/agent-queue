import { describe, expect, it } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useDirtyForm } from "../useDirtyForm";

describe("useDirtyForm", () => {
  it("starts clean", () => {
    const { result } = renderHook(() => useDirtyForm({ name: "a" }));
    expect(result.current.dirty).toBe(false);
    expect(result.current.value).toEqual({ name: "a" });
  });

  it("becomes dirty when value diverges from baseline (deep comparison)", () => {
    const { result } = renderHook(() => useDirtyForm({ name: "a", tags: ["x"] }));
    act(() => {
      result.current.setValue({ name: "a", tags: ["x", "y"] });
    });
    expect(result.current.dirty).toBe(true);
  });

  it("resetBaseline clears dirty and adopts the new value", () => {
    const { result } = renderHook(() => useDirtyForm({ name: "a" }));
    act(() => {
      result.current.setValue({ name: "b" });
    });
    expect(result.current.dirty).toBe(true);
    act(() => {
      result.current.resetBaseline({ name: "c" });
    });
    expect(result.current.dirty).toBe(false);
    expect(result.current.value).toEqual({ name: "c" });
  });
});
