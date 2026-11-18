/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// D83: in development the browser talks only to Vite on 127.0.0.1:5173, which forwards
// /api to the API on 127.0.0.1:8000. UI and API share one origin, so the session cookie
// and SameSite=Strict behave as in production and no CORS is needed. The Host header is
// kept (no changeOrigin), so the API's Origin check (D82) sees its own origin.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: { "/api": { target: "http://127.0.0.1:8000" } },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    restoreMocks: true,
    unstubGlobals: true,
  },
});
