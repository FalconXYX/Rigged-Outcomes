// The browser the scenarios drive: Chromium through playwright-core. Where they are can be set with
// E2E_PLAYWRIGHT (the playwright-core package folder) and E2E_CHROME (the browser binary); the defaults are
// this Mac's (the npx cache's playwright-core and Playwright's headless shell, or full Chrome for --headed).

import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import { homedir } from "node:os";
import { join } from "node:path";

const require = createRequire(import.meta.url);
const PW_DEFAULT = join(homedir(), ".npm/_npx/9833c18b2d85bc59/node_modules/playwright-core");
const CACHE = join(homedir(), "Library/Caches/ms-playwright");
const SHELL = join(CACHE, "chromium_headless_shell-1248/chrome-headless-shell-mac-arm64/chrome-headless-shell");
const CHROME = join(CACHE, "chromium-1248/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing");

function playwright() {
  for (const where of [process.env.E2E_PLAYWRIGHT, "playwright-core", PW_DEFAULT]) {
    if (!where) continue;
    try {
      return require(where);
    } catch {
      /* the next one */
    }
  }
  throw new Error("playwright-core was not found: set E2E_PLAYWRIGHT to its package folder (npx playwright-core install puts one in the npx cache)");
}

/** A launched Chromium: headless by default, the full browser with headed. */
export async function launch({ headed = false } = {}) {
  const { chromium } = playwright();
  const executablePath = process.env.E2E_CHROME || [headed ? CHROME : SHELL, CHROME, SHELL].find((p) => existsSync(p));
  return chromium.launch({ headless: !headed, ...(executablePath ? { executablePath } : {}) });
}
