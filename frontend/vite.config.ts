/// <reference types="vitest/config" />
/// <reference types="node" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The dashboard talks to the API through this dev proxy, so the browser only
// ever sees one origin. Inside Docker Compose the backend host is "backend".
const backendUrl = process.env.ATLAS_BACKEND_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: process.env.ATLAS_FRONTEND_HOST ?? "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": backendUrl,
      "/health": backendUrl,
      "/ready": backendUrl,
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    restoreMocks: true,
  },
});
