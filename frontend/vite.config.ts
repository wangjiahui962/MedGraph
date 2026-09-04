import { defineConfig } from "vite";
export default defineConfig({
  base: "./",
  server: {
    // 把 /api 请求代理到本地后端服务（server.py，默认 127.0.0.1:8756），
    // 前端点击“增加新数据 / 提取现有数据”时通过它触发采集与抽取。
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8756",
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: { output: { manualChunks: { graph: ["cytoscape"] } } },
  },
});