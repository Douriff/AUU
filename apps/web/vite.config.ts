import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

// Production build hardening: no source maps, minified + mangled identifiers, no
// comments/license banners, console.log/info/debug and `debugger` stripped. This only
// raises the cost of reading the bundle; front-end code can always be inspected in the
// browser, so nothing secret (keys, internal hosts, strategy logic) may live in it —
// strategy runs on the server only.
export default defineConfig(({ mode }) => ({
  plugins: [react()],
  build: {
    sourcemap: false,
    minify: "esbuild",
    cssMinify: true,
    reportCompressedSize: false,
  },
  esbuild:
    mode === "production"
      ? {
          drop: ["debugger"],
          pure: ["console.log", "console.info", "console.debug", "console.trace"],
          legalComments: "none",
          minifyIdentifiers: true,
          minifySyntax: true,
          minifyWhitespace: true,
        }
      : undefined,
  define: {
    global: "globalThis",
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
      buffer: "buffer/",
    },
  },
  optimizeDeps: {
    include: ["buffer", "three", "3d-shape-particle-text"],
  },
  server: {
    port: 5173,
    host: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        ws: true,
      },
    },
  },
}));
