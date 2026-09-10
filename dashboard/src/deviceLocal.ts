/**
 * The dashboard's only door to browser persistence.
 *
 * Persistent feature state — anything a user expects to find again after a
 * reload, on this machine or another — belongs on the server
 * (`useDashboardDocument`, docs/superpowers/specs/2026-09-10-dashboard-state-contract-design.md).
 * Addressable navigation belongs in the URL; everything else lives in memory.
 *
 * The keys below are the narrow exceptions: device-local transport state with
 * no meaning on another device, each documented in `dashboard/CLAUDE.md`.
 * `tests/test_dashboard_browser_storage.py` fails CI if any other production
 * module touches browser storage, or if this registry gains a key that is not
 * documented there.
 */
export const DEVICE_LOCAL_KEYS = {
  "aq:ws:last_seq": "WebSocket replay cursor; missing or stale means replay or refetch",
  "aq:ws:epoch": "event-stream epoch that invalidates the local replay cursor",
  "aq:session:id": "read-only connection identity for the console-stream scope check",
} as const;

export type DeviceLocalKey = keyof typeof DEVICE_LOCAL_KEYS;

// Storage can throw (private mode, quota, a sandboxed frame). Every key here is
// recoverable by reconnecting, so a failure reads as absent and writes are dropped.

export function readDeviceLocal(key: DeviceLocalKey): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeDeviceLocal(key: DeviceLocalKey, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* ignore */
  }
}

export function removeDeviceLocal(key: DeviceLocalKey): void {
  try {
    window.localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}
