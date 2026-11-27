import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the React dev server forwards /api calls to the Flask backend.
export default defineConfig({
  plugins: [react()],
  server: { host: true, proxy: { "/api": "http://127.0.0.1:5050" } },
});
