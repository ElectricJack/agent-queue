import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";
import { ShortcutsProvider, useShortcut } from "../useShortcuts";

function Probe({ onFire }: { onFire: () => void }) {
  useShortcut("$mod-k", { label: "open palette", onFire });
  return <div>probe</div>;
}

describe("useShortcut $mod normalization", () => {
  test("normalizes to Cmd-K on mac", async () => {
    Object.defineProperty(navigator, "userAgent", {
      value: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
      configurable: true,
    });
    const spy = vi.fn();
    render(
      <ShortcutsProvider>
        <Probe onFire={spy} />
      </ShortcutsProvider>,
    );
    await userEvent.keyboard("{Meta>}k{/Meta}");
    expect(spy).toHaveBeenCalled();
  });

  test("normalizes to Ctrl-K on linux", async () => {
    Object.defineProperty(navigator, "userAgent", {
      value: "Mozilla/5.0 (X11; Linux x86_64)",
      configurable: true,
    });
    const spy = vi.fn();
    render(
      <ShortcutsProvider>
        <Probe onFire={spy} />
      </ShortcutsProvider>,
    );
    await userEvent.keyboard("{Control>}k{/Control}");
    expect(spy).toHaveBeenCalled();
  });
});
