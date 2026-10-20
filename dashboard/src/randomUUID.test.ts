import { afterEach, describe, expect, it, vi } from "vitest";
import { randomUUID } from "./randomUUID";

const V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const original = globalThis.crypto;

function stubCrypto(value: unknown) {
  Object.defineProperty(globalThis, "crypto", { value, configurable: true, writable: true });
}

afterEach(() => {
  stubCrypto(original);
  vi.restoreAllMocks();
});

describe("randomUUID", () => {
  it("delegates to the native generator in a secure context", () => {
    stubCrypto({ randomUUID: () => "11111111-1111-4111-8111-111111111111", getRandomValues: () => { throw new Error("unused"); } });
    expect(randomUUID()).toBe("11111111-1111-4111-8111-111111111111");
  });

  it("builds a v4 UUID from getRandomValues when randomUUID is absent (plain HTTP)", () => {
    stubCrypto({ getRandomValues: (bytes: Uint8Array) => original.getRandomValues(bytes) });
    const ids = new Set(Array.from({ length: 50 }, () => randomUUID()));
    expect(ids.size).toBe(50);
    for (const id of ids) expect(id).toMatch(V4);
  });

  it("sets the version and variant bits over all-ones random bytes", () => {
    stubCrypto({ getRandomValues: (bytes: Uint8Array) => bytes.fill(0xff) });
    expect(randomUUID()).toBe("ffffffff-ffff-4fff-bfff-ffffffffffff");
  });

  it("still returns a v4 UUID with no crypto at all", () => {
    stubCrypto(undefined);
    expect(randomUUID()).toMatch(V4);
  });
});
