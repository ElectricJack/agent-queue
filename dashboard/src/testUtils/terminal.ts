import { vi } from "vitest";
import type { ITerminalOptions } from "@xterm/xterm";

export class TerminalSocketMock {
  static instances: TerminalSocketMock[] = [];
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  readyState = 0;
  binaryType = "blob";
  bufferedAmount = 0;
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string | ArrayBuffer }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  send = vi.fn<(data: string | Uint8Array) => void>();
  constructor(public url: string, public protocols?: string[]) { TerminalSocketMock.instances.push(this); }
  open() { this.readyState = 1; this.onopen?.(); }
  ready() {
    this.open();
    this.message(JSON.stringify({
      type: "ready", session_id: decodeURIComponent(new URL(this.url).pathname.split("/").slice(-1)[0]!),
      cols: Number(new URL(this.url).searchParams.get("cols")), rows: Number(new URL(this.url).searchParams.get("rows")),
    }));
  }
  message(data: string | Uint8Array) {
    this.onmessage?.({ data: typeof data === "string" ? data : Uint8Array.from(data).buffer });
  }
  serverClose() { this.readyState = 3; this.closed = true; this.onclose?.(); }
  close = vi.fn(() => this.serverClose());
  inputs() { return this.send.mock.calls.map(([data]) => data).filter((data): data is Uint8Array => typeof data !== "string"); }
  controls() { return this.send.mock.calls.flatMap(([data]) => typeof data === "string" ? [JSON.parse(data)] : []); }
}

export class TerminalMock {
  static instances: TerminalMock[] = [];
  options: ITerminalOptions;
  cols = 80;
  rows = 24;
  textarea?: HTMLTextAreaElement;
  element?: HTMLDivElement;
  output?: HTMLPreElement;
  disposed = false;
  selection = "";
  dataHandlers = new Set<(data: string) => void>();
  binaryHandlers = new Set<(data: string) => void>();
  resizeHandlers = new Set<(size: { cols: number; rows: number }) => void>();
  keyHandler?: (event: KeyboardEvent) => boolean;
  pendingWrites: (() => void)[] = [];
  parser = { registerOscHandler: vi.fn(() => ({ dispose: vi.fn() })) };
  constructor(options: ITerminalOptions) { this.options = options; TerminalMock.instances.push(this); }
  loadAddon(addon: FitAddonMock) { addon.activate(this); }
  open(host: HTMLElement) {
    this.element = document.createElement("div");
    this.textarea = document.createElement("textarea");
    this.output = document.createElement("pre");
    this.element.append(this.textarea, this.output);
    host.append(this.element);
  }
  onData(handler: (data: string) => void) {
    this.dataHandlers.add(handler);
    return { dispose: () => this.dataHandlers.delete(handler) };
  }
  onBinary(handler: (data: string) => void) {
    this.binaryHandlers.add(handler);
    return { dispose: () => this.binaryHandlers.delete(handler) };
  }
  onResize(handler: (size: { cols: number; rows: number }) => void) {
    this.resizeHandlers.add(handler);
    return { dispose: () => this.resizeHandlers.delete(handler) };
  }
  attachCustomKeyEventHandler(handler: (event: KeyboardEvent) => boolean) { this.keyHandler = handler; }
  emitData(data: string) { this.dataHandlers.forEach((handler) => handler(data)); }
  emitBinary(data: string) { this.binaryHandlers.forEach((handler) => handler(data)); }
  write = vi.fn((data: Uint8Array, processed: () => void) => {
    if (this.output) this.output.textContent += new TextDecoder().decode(data);
    this.pendingWrites.push(processed);
  });
  flushWrites() { this.pendingWrites.splice(0).forEach((done) => done()); }
  resize(cols: number, rows: number) {
    this.cols = cols; this.rows = rows;
    this.resizeHandlers.forEach((handler) => handler({ cols, rows }));
  }
  hasSelection() { return !!this.selection; }
  focus = vi.fn(() => this.textarea?.focus({ preventScroll: true }));
  dispose = vi.fn(() => {
    this.disposed = true; this.element?.remove();
    this.dataHandlers.clear(); this.binaryHandlers.clear(); this.resizeHandlers.clear();
  });
}

export class FitAddonMock {
  static instances: FitAddonMock[] = [];
  dimensions = { cols: 80, rows: 24 };
  constructor() { FitAddonMock.instances.push(this); }
  terminal?: TerminalMock;
  activate(terminal: TerminalMock) { this.terminal = terminal; }
  proposeDimensions() { return this.dimensions; }
  dispose = vi.fn();
}

export class ResizeObserverMock {
  static instances: ResizeObserverMock[] = [];
  constructor(private callback: ResizeObserverCallback) { ResizeObserverMock.instances.push(this); }
  observe = vi.fn();
  disconnect = vi.fn();
  emit() { this.callback([], this as unknown as ResizeObserver); }
}
