import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  // Read only frontend/.env; backend secrets never enter the browser bundle.
  const env = loadEnv(mode, process.cwd(), "");
  const target =
    process.env.OPSPILOT_API_TARGET ||
    env.OPSPILOT_API_TARGET ||
    "http://127.0.0.1:8000";
  const proxy = {
    "/api": { target, changeOrigin: false },
    "/health": { target, changeOrigin: false },
  };
  return {
    plugins: [react()],
    server: { host: "127.0.0.1", port: 5173, strictPort: true, proxy },
    preview: { host: "127.0.0.1", port: 4173, strictPort: true, proxy },
  };
});
