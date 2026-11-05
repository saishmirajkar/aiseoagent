import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development, API calls are proxied to the FastAPI server on :8000.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://localhost:8000" } },
  build: { outDir: "dist", sourcemap: false },
});
