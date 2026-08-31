import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const apiTarget = process.env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/auth": apiTarget,
      "/me": apiTarget,
      "/accounts": apiTarget,
      "/markets": apiTarget,
      "/orders": apiTarget,
    },
  },
});
