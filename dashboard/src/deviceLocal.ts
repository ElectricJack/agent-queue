/**
 * The dashboard's only door to browser persistence.
 *
 * Persistent feature state — anything a user expects to find again after a
 * reload, on this machine or another — belongs on the server
 * (`useDashboardDocument`, docs/superpowers/specs/2026-09-10-dashboard-state-contract-design.md).
 * Addressable navigation belongs in the URL; everything else lives in memory.
 *
 * The keys below are the narrow exceptions: device-local transport state and
 * four Metrics collapse preferences explicitly requested per browser by Jack
 * on 2026-10-27 (smart-harbor-20), documented in `dashboard/AGENTS.md`.
 * `tests/test_dashboard_browser_storage.py` fails CI if any other production
 * module touches browser storage, or if this registry gains a key that is not
 * documented there.
 */
export const DEVICE_LOCAL_KEYS = {
  "aq:ws:last_seq": "WebSocket replay cursor; missing or stale means replay or refetch",
  "aq:ws:epoch": "event-stream epoch that invalidates the local replay cursor",
  "aq:session:id": "read-only connection identity for the console-stream scope check",
} as const;

export const METRICS_SECTION_KEYS = {
  "aq:metrics:stat-tiles:open": "Stat tiles section disclosure",
  "aq:metrics:graphs:open": "Graphs section disclosure",
  "aq:metrics:provider-usage:open": "Provider usage section disclosure",
  "aq:metrics:delivery-latency:open": "Delivery latency section disclosure",
} as const;

export type MetricsSectionKey = keyof typeof METRICS_SECTION_KEYS;
export type DeviceLocalKey = keyof typeof DEVICE_LOCAL_KEYS | MetricsSectionKey;

// Storage can throw (private mode, quota, a sandboxed frame). Every key here is
// recoverable by reconnecting or using the default disclosure state, so a
// failure reads as absent and writes are dropped.

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
