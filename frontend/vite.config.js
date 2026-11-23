import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the SPA and the API share one origin through this proxy, so the session cookie and
// CSRF token work exactly as they will behind nginx in production.
const backend = process.env.VITE_BACKEND_URL || "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": backend, "/django-admin": backend, "/static": backend },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    // No manual chunking: the chart library is only reached through a lazy import (RevenueChart), so the
    // bundler splits it into its own async chunk and roles that never see revenue never download it.
    // (A manualChunks rule pulled react-dom into that chunk and made it load eagerly.)
    chunkSizeWarningLimit: 600,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.js",
    css: false,
  },
});
