// Vite build settings. `base: "./"` makes every URL relative, so the same build works on
// GitHub Pages (served from /<repo>/) and from `npm run preview` at the site root.
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "./",
  plugins: [react()],
});
