import {defineConfig, loadEnv} from "vite";
import vendorMapPwa from "./tools/pwa-build.mjs";

export default defineConfig(({mode}) => {
  const env = loadEnv(mode, process.cwd(), "API_PROXY_");
  const apiBase = (process.env.VITE_API_BASE ?? loadEnv(mode, process.cwd(), "VITE_").VITE_API_BASE ?? "").trim();
  if (process.env.VERCEL === "1" && apiBase && apiBase !== "/") {
    throw new Error("On Vercel, remove VITE_API_BASE or leave it empty so /api uses vercel.json's first-party session proxy.");
  }
  const proxy = {
    "/api": {
      target: env.API_PROXY_TARGET || "http://127.0.0.1:8000",
      changeOrigin: true,
    },
  };
  return {
    plugins: [vendorMapPwa()],
    server: {host: "0.0.0.0", proxy},
    preview: {host: "0.0.0.0", proxy},
    build: {
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (/\/node_modules\/(react|react-dom|scheduler)\//.test(id)) return "react-vendor";
          },
        },
      },
    },
  };
});
