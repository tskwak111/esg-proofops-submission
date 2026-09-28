import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, "../..", "");
  const api = env.API_PUBLIC_BASE_URL || "http://localhost:8000";
  return {
    envDir: "../..",
    plugins: [react()],
    server: {
      port: 5173,
      proxy: { "/v1": api, "/auth": api, "/local/uploads": api, "/local/sources": api },
    },
  };
});
