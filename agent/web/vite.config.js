import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Rutas relativas (base: "./"): necesario para que los assets funcionen
// bien sirviendo desde GitHub Pages.
//
// outDir queda en el default de Vite ("dist") a proposito: el deploy no
// commitea el build al repo, lo hace el workflow de GitHub Actions
// (deploy-web.yml) subiendolo directo a Pages en cada push - ver ese
// archivo para el detalle.
export default defineConfig({
  plugins: [react()],
  base: "./",
});
