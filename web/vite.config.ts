import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API runs separately in development (`uvicorn app:app --port 8000`); the built
// app is served by the API itself, so only the dev server needs the proxy.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": "http://localhost:8000" },
  },
  build: { outDir: "dist", sourcemap: false },
});
