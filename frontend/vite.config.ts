import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发期把 /api 代理到后端 FastAPI(默认 8001),避免 CORS 与 SSE 跨域问题。
const BACKEND = process.env.VITE_BACKEND || "http://localhost:8001";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      "/api": {
        target: BACKEND,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
});
