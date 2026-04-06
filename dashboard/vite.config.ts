import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8081",
      "/health": "http://127.0.0.1:8081",
      "/ready": "http://127.0.0.1:8081",
      "/ws": { target: "http://127.0.0.1:8081", ws: true },
    },
  },
});
