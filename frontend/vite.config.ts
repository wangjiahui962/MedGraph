import { defineConfig } from "vite";
export default defineConfig({
  base: "./",
  build: {
    rollupOptions: { output: { manualChunks: { graph: ["cytoscape"] } } },
  },
});
