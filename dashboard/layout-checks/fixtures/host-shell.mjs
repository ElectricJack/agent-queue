// The host shell page (/host-shell): a login shell on the machine running AQ,
// for remote management. It is not an agent — no task, no session row — and it
// draws its terminal with the same component the phone's agent terminal draws
// (dashboard/src/components/InteractiveTerminal.tsx), which is what the
// `terminal-parity` check compares.
export const HOST_SHELL = "aq-host-shell-1";

/** @satisfies {import("@aq/ts-client").HostShellListResponse} */
const shells = { enabled: true, shells: [{ name: HOST_SHELL, created_at: 1_790_000_000, attached_clients: 1 }] };

export const routes = {
  "GET /api/host-shell": () => shells,
  "POST /api/host-shell": () => ({ shell: shells.shells[0] }),
  [`POST /api/host-shell/${HOST_SHELL}/close`]: () => ({ name: HOST_SHELL, closed: true }),
};
