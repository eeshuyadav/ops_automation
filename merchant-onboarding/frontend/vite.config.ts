import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  server: {
    port: 5173,
    // Vite 5+ blocks Host headers it doesn't recognize as a security
    // measure (CVE-2024-23331 mitigation). When nginx terminates TLS at
    // merchant-onboarding.gokwik.io and forwards with that Host header,
    // Vite rejects with 403 unless we whitelist it here.
    allowedHosts: [
      "localhost",
      "merchant-onboarding.gokwik.io",
      "merchant-onboarding.gokwik.co",
      "10.10.53.224",
    ],
    proxy: {
      "/api": {
        target: "http://localhost:8001",
        changeOrigin: true,
      },
    },
  },
});
