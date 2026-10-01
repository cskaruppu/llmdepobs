import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development, /api is proxied to a local API started with `tensorhelm-api` (TH_AUTH_MODE=dev).
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://localhost:8000" } },
  build: { outDir: "dist", sourcemap: false },
});
