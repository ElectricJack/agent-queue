import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Read via globalThis so this config typechecks without @types/node.
const target =
  (globalThis as { process?: { env: Record<string, string | undefined> } }).process?.env
    .AQ_API_TARGET ?? "http://127.0.0.1:8081";

export default defineConfig({
  // Served at the root everywhere: by this dev server, and by the dashboard
  // server in an install (docs/specs/dashboard-server.md §4).
  base: "/",
  plugins: [react(), tailwindcss()],
  server: {
    // Bind all interfaces, not just loopback: under WSL2 a Windows browser can
    // arrive over the VM's IP (e.g. a netsh portproxy rule), which never reaches
    // a 127.0.0.1-only listener.
    host: true,
    port: 5173,
    proxy: {
      "/api": {
        target,
        configure(proxy) {
          proxy.on("proxyReq", (request, incoming) => {
            // Dev proxy peers must not forge the production dashboard's
            // operator verdict before the daemon sees their loopback hop.
            const peer = incoming.socket.remoteAddress;
            const local = peer === "127.0.0.1" || peer === "::1"
              || peer === "::ffff:127.0.0.1";
            request.setHeader("x-aq-dashboard-viewer", local ? "operator" : "other");
          });
        },
      },
      "/health": target,
      "/ready": target,
      "/ws": { target, ws: true },
    },
  },
});
