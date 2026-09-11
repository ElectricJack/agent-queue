import { fireEvent, render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, test, vi } from "vitest";
import { ShortcutsProvider, useShortcut } from "../useShortcuts";

const originalUserAgent = Object.getOwnPropertyDescriptor(navigator, "userAgent");
afterEach(() => {
  if (originalUserAgent) Object.defineProperty(navigator, "userAgent", originalUserAgent);
  else Reflect.deleteProperty(navigator, "userAgent");
});

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

function KeyProbe({ keys, onFire }: { keys: string; onFire: () => void }) {
  useShortcut(keys, { label: "probe", onFire });
  return <div>probe</div>;
}

function renderKey(keys: string) {
  const spy = vi.fn();
  render(
    <ShortcutsProvider>
      <KeyProbe keys={keys} onFire={spy} />
    </ShortcutsProvider>,
  );
  return spy;
}

// react-hotkeys-hook 5 compares a hotkey with the event's `code` unless told to
// use `key`, and `code` names the physical key: "[" arrives as BracketLeft.
describe("useShortcut punctuation", () => {
  test("a binding written as '[' fires when [ is pressed", async () => {
    const spy = renderKey("[");
    await userEvent.keyboard("[BracketLeft]");
    expect(spy).toHaveBeenCalledTimes(1);
  });

  test("a binding written as ']' fires when ] is pressed", async () => {
    const spy = renderKey("]");
    await userEvent.keyboard("[BracketRight]");
    expect(spy).toHaveBeenCalledTimes(1);
  });

  test("a binding written as '/' fires for the browser's Slash key", () => {
    const spy = renderKey("/");
    fireEvent.keyDown(document, { key: "/", code: "Slash" });
    expect(spy).toHaveBeenCalledTimes(1);
  });

  test("a binding named by code still fires", async () => {
    const spy = renderKey("meta+bracketleft");
    await userEvent.keyboard("{Meta>}[BracketLeft]{/Meta}");
    expect(spy).toHaveBeenCalledTimes(1);
  });

  test("a character binding ignores a different key on the same code", () => {
    const spy = renderKey("[");
    fireEvent.keyDown(document, { key: "{", code: "BracketLeft", shiftKey: true });
    expect(spy).not.toHaveBeenCalled();
  });
});
