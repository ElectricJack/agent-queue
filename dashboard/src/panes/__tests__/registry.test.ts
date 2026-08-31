import { afterAll, vi } from "vitest";

vi.hoisted(() => {
  class Socket {
    static OPEN = 1;
    static CONNECTING = 0;
    readyState = 0;
    close() { this.readyState = 3; }
  }
  vi.stubGlobal("WebSocket", Socket);
});
afterAll(() => vi.unstubAllGlobals());

import { PANE_REGISTRY } from "../registry";

test("every registered entry has a manifest whose id matches the key", () => {
  for (const [key, entry] of Object.entries(PANE_REGISTRY)) {
    expect(entry.manifest.id).toBe(key);
  }
});

test("no open_shortcut collisions across views", () => {
  const seen = new Map<string, string>();
  for (const entry of Object.values(PANE_REGISTRY)) {
    const s = entry.manifest.open_shortcut;
    if (!s) continue;
    if (seen.has(s)) {
      throw new Error(
        `open_shortcut collision: ${entry.manifest.id} and ${seen.get(s)} both bind ${s}`,
      );
    }
    seen.set(s, entry.manifest.id);
  }
});

test("no manifest uses literal null for open_shortcut", () => {
  for (const entry of Object.values(PANE_REGISTRY)) {
    expect(entry.manifest.open_shortcut).not.toBeNull();
  }
});

test("useAgentPushBridge is a named export from agentPush", async () => {
  const mod = await import("../agentPush");
  expect(typeof mod.useAgentPushBridge).toBe("function");
});
