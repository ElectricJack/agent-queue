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

import { Suspense } from "react";
import { render, screen } from "@testing-library/react";
import { PANE_REGISTRY } from "../registry";

test("stub-smoke view is registered and renders", async () => {
  const entry = PANE_REGISTRY["__stub-smoke"];
  expect(entry).toBeDefined();
  const { Component } = entry!;
  render(
    <Suspense fallback={null}>
      <Component
        args={{ text: "hello pane" }}
        close={() => {}}
        setArgs={() => {}}
        setToolbar={() => {}}
        setShortcuts={() => {}}
      />
    </Suspense>,
  );
  expect(await screen.findByTestId("stub-smoke")).toHaveTextContent("hello pane");
});
