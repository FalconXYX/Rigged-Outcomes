import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Relative base + hash routing: the build works from any GitHub Pages
// sub-path (parthjain.ca/<repo>/) without knowing the repo name.
export default defineConfig({
  base: "./",
  plugins: [react()],
});
