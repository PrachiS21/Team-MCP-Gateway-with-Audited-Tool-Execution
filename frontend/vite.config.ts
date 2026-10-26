import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the dashboard runs on Vite's server and forwards /api and /mcp to a gateway running locally,
// so the browser still sees ONE origin (as in the cluster, where Ingress puts all three on the same host).
const gateway = process.env.GATEWAY_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": gateway, "/mcp": gateway } },
});
