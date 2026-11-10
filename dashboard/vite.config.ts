import path from "path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vitest/config"

export default defineConfig({
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 3000,
    proxy: {
      "/api": {
        target: "http://localhost:8080",
        changeOrigin: true,
      },
    },
  },
  build: {
    rollupOptions: {
      output: {
        // Vite 8 (Rolldown) only accepts the function form of manualChunks.
        manualChunks(id) {
          if (!id.includes("node_modules")) return
          if (/[\\/]node_modules[\\/](react|react-dom|react-router-dom)[\\/]/.test(id)) return "vendor"
          if (/[\\/]node_modules[\\/]@tanstack[\\/]react-query[\\/]/.test(id)) return "query"
          if (/[\\/]node_modules[\\/]recharts[\\/]/.test(id)) return "charts"
          if (/[\\/]node_modules[\\/](lucide-react|sonner|zustand|ky|date-fns)[\\/]/.test(id)) return "ui"
        },
      },
    },
  },
})
