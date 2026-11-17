import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [svelte()],
  server: {
    proxy: { "/api": process.env.PM_API ?? "http://127.0.0.1:8000" },
  },
  build: { target: "es2022" },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
