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
    rollupOptions: {
      output: {
        // Separate, long-cached vendor chunks: app deploys don't invalidate React or the chart library.
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom", "@tanstack/react-query"],
          charts: ["recharts"],
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.js",
    css: false,
  },
});
