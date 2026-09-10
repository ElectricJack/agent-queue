import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Read via globalThis so this config typechecks without @types/node.
const target =
  (globalThis as { process?: { env: Record<string, string | undefined> } }).process?.env
    .AQ_API_TARGET ?? "http://127.0.0.1:8081";
const embedded =
  (globalThis as { process?: { env: Record<string, string | undefined> } }).process?.env
    .AQ_DASHBOARD_EMBEDDED === "1";

export default defineConfig({
  base: embedded ? "/dashboard/" : "/",
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": target,
      "/health": target,
      "/ready": target,
      "/ws": { target, ws: true },
    },
  },
});
