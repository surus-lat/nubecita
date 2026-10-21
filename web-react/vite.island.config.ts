import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build de la isla: IIFE de un solo archivo → ../web/react-island.js, para
// que el SPA vanilla la cargue con un <script> clásico (sin ESM).
// No toca el build del scaffold (vite.config.ts sigue con su propio 'build').
export default defineConfig({
  plugins: [react()],
  define: { "process.env.NODE_ENV": '"production"' },
  build: {
    lib: {
      entry: "src/island.ts",
      formats: ["iife"],
      name: "NubecitaIsla",
      fileName: () => "react-island.js",
    },
    outDir: "../web",
    emptyOutDir: false, // NO borrar web/ (index.html vive ahí)
    cssCodeSplit: false, // el CSS va inlined vía ?inline → un solo .js
  },
});
