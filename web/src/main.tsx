import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/montserrat";
import "./styles/tokens.css";
import "./styles/base.css";
import "./components/ui.css";
import "./components/shell.css";
import { initTheme } from "./lib/theme";
import App from "./App";

initTheme();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
