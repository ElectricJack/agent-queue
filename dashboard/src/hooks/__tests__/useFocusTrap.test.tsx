import { describe, expect, it, vi } from "vitest";
import { useRef, useState } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { useFocusTrap } from "../useFocusTrap";

function Dialog({ onEscape }: { onEscape: () => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useFocusTrap(ref, open, { onEscape: () => { onEscape(); setOpen(false); } });
  return (
    <>
      <button onClick={() => setOpen(true)}>opener</button>
      {open && (
        <div ref={ref} tabIndex={-1}>
          <button>first</button>
          <button>last</button>
        </div>
      )}
    </>
  );
}

describe("useFocusTrap", () => {
  it("cycles Tab, closes on Escape and returns focus to the opener", () => {
    const onEscape = vi.fn();
    render(<Dialog onEscape={onEscape} />);
    const opener = screen.getByRole("button", { name: "opener" });
    opener.focus();
    act(() => opener.click());
    screen.getByRole("button", { name: "last" }).focus();
    fireEvent.keyDown(document, { key: "Tab" });
    expect(screen.getByRole("button", { name: "first" })).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onEscape).toHaveBeenCalledOnce();
    expect(opener).toHaveFocus();
  });
});
