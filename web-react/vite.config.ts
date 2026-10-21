import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// base './' → assets relativos: el dist/ puede servirse desde cualquier
// sub-path de uvicorn sin reescribir rutas (mismo contrato que web/index.html).
export default defineConfig({
  base: "./",
  plugins: [react()],
  build: {
    outDir: "dist",
  },
});
