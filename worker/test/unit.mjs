// npm run unit: the Worker's pure pieces, in a second and with no Worker running: the wake-up signature (the
// contract's test vector), the wake-up back-off, the error retry waits, the budget levels, Toronto time across a
// change of clocks, the daily limits' day and where wake-ups may go. src/ is bundled with the esbuild that
// comes with wrangler. Exits 1 on any failed check.

import { build } from "esbuild";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const entry = `
export { sign, wakeHeader, backoff, stepUrl } from "./src/wake";
export { levelOf, readBudget, blockedText, capsOf } from "./src/ledger";
export { retryDelay } from "./src/queue";
export { limitDay, daysBefore } from "./src/lib";
export { CONTRACT, toronto, torontoAt, dayBefore, hhmm } from "./src/contract";
`;
const out = await build({ stdin: { contents: entry, resolveDir: ROOT, loader: "ts" }, bundle: true, format: "esm", platform: "neutral", write: false });
const m = await import(`data:text/javascript;base64,${Buffer.from(out.outputFiles[0].text).toString("base64")}`);

let passed = 0;
const failed = [];
const check = (name, ok, detail) => {
  if (ok) passed++;
  else {
    failed.push(name);
    console.log(`FAIL ${name}${detail === undefined ? "" : `\n     ${JSON.stringify(detail)}`}`);
  }
};

// the wake-up's signature: the same vector as tests/test_backend_contract.py
const sig = await m.sign("test-wake-secret", 1795078800, "main", "0123456789abcdef");
check("wake: the contract's test vector", sig === "20ef9719fc110b70bbb7dd2f327e5b4e0c3d07117b936549707287c366a7304a", sig);
const header = await m.wakeHeader("test-wake-secret", "news", 1795078800, "0123456789abcdef");
check("wake: the header is v1,<ts>,<lane>,<nonce>,<sig>", /^v1,1795078800,news,0123456789abcdef,[0-9a-f]{64}$/.test(header), header);
check("wake: another lane signs differently", header.split(",")[4] !== sig);

// back-off: 0, then 60 × 2^(n−1) up to an hour
check("backoff: nothing unanswered waits 0", m.backoff(0) === 0);
check("backoff: 1, 2, 3 unanswered wait 60, 120, 240 s", m.backoff(1) === 60 && m.backoff(2) === 120 && m.backoff(3) === 240);
check("backoff: at most an hour", m.backoff(7) === 3600 && m.backoff(40) === 3600, [m.backoff(7), m.backoff(40)]);

// an error's retry: min(30 × 2^(attempt−1), 1800) plus 0..15 s
const waits = (a) => Array.from({ length: 200 }, () => m.retryDelay(a));
const w1 = waits(1);
const w6 = waits(8);
check("retry: the first waits 30 to 45 s", w1.every((s) => s >= 30 && s <= 45) && new Set(w1).size > 5, [Math.min(...w1), Math.max(...w1)]);
check("retry: the fifth waits 480 to 495 s", waits(5).every((s) => s >= 480 && s <= 495));
check("retry: capped at 1800 + 15 s", w6.every((s) => s >= 1800 && s <= 1815), [Math.min(...w6), Math.max(...w6)]);

// budget levels: the share of the month, or today's own caps
const B = m.CONTRACT.budget;
check("budget: nothing used is ok", m.levelOf(0, 0, 0, 0, 0) === "ok");
check("budget: half the CPU is tight", m.levelOf(B.cpu_ms_month * 0.5, 0, 0, 0, 0) === "tight");
check("budget: 65% of the GB-hours is low", m.levelOf(0, B.gb_ms_month * 0.65, 0, 0, 0) === "low");
check("budget: 80% of the invocations is stop", m.levelOf(0, 0, B.invocations_month * 0.8, 0, 0) === "stop");
check("budget: 20 CPU-minutes in one day is stop", m.levelOf(0, 0, 0, 1_200_000, 0) === "stop" && m.levelOf(0, 0, 0, 1_199_999, 0) === "ok");
check("budget: 30 GB-hours in one day is stop", m.levelOf(0, 0, 0, 0, 108_000_000) === "stop");
check("budget: none saved reads as ok, computed at 0", m.readBudget(null).level === "ok" && m.readBudget("{bad").computed_at === 0);
check("budget: a saved one reads back", m.readBudget(JSON.stringify({ level: "low", cpu_ms_30d: 5 })).cpu_ms_30d === 5);
check("budget: an unknown level reads as ok", m.readBudget(JSON.stringify({ level: "panic" })).level === "ok");
check("budget: blocked kinds as the claim's text", m.blockedText("ok") === "" && m.blockedText("low").startsWith(",add.fetch,") && m.blockedText("stop") === null);
check("budget: caps per level", m.capsOf("ok").add_whales_day === 800 && m.capsOf("tight").open_reads === 15 && m.capsOf("low").catchup === false);

// Toronto time: 04:00 is 09:00 UTC in winter and 08:00 UTC in summer, and a day is 23 or 25 hours on a change
const utc = (s) => Date.parse(s) / 1000;
check("toronto: 04:00 in winter is 09:00 UTC", m.torontoAt("2026-11-20", "04:00") === utc("2026-11-20T09:00:00Z"));
check("toronto: 04:00 in summer is 08:00 UTC", m.torontoAt("2027-03-15", "04:00") === utc("2027-03-15T08:00:00Z"));
check("toronto: 09:30 the day clocks go forward", m.torontoAt("2027-03-14", "09:30") === utc("2027-03-14T13:30:00Z"));
check("toronto: 04:00 the day clocks go back", m.torontoAt("2026-11-01", "04:00") === utc("2026-11-01T09:00:00Z"));
const at = m.toronto(utc("2026-11-20T04:59:00Z"));
check("toronto: 04:59 UTC is still the day before, 23:59", at.day === "2026-11-19" && at.minutes === 23 * 60 + 59, at);
check("toronto: midnight Toronto starts the day", m.toronto(utc("2026-11-20T05:00:00Z")).day === "2026-11-20");
check("toronto: a Toronto day that is a UTC day later", m.toronto(utc("2027-07-01T03:30:00Z")).day === "2027-06-30");
check("days: the day before, across a month", m.dayBefore("2026-12-01") === "2026-11-30" && m.daysBefore("2027-03-01", 29) === "2027-01-31");
check("days: hh:mm", m.hhmm("04:00") === 240 && m.hhmm("12:00") === 720);

// the daily limits count per Toronto day (X-Test-Day stands in only with DEV=on)
const req = (h = {}) => new Request("https://x/", { headers: h });
check("limits: the Toronto day", m.limitDay(req(), { DEV: "off" }, utc("2026-11-20T04:00:00Z")) === "2026-11-19");
check("limits: X-Test-Day with DEV=on", m.limitDay(req({ "x-test-day": "t-1" }), { DEV: "on" }, 0) === "t-1");
check("limits: X-Test-Day ignored with DEV off", m.limitDay(req({ "x-test-day": "t-1" }), { DEV: "off" }, utc("2026-11-20T12:00:00Z")) === "2026-11-20");

// where wake-ups go: https in production, only a local address with DEV=on, nowhere without a secret
const env = (url, dev, secret = "s") => ({ STEP_URL: url, DEV: dev, WAKE_SECRET: secret });
check("step: production https", m.stepUrl(env("https://rigged-outcomes.vercel.app/api/step", "off")) === "https://rigged-outcomes.vercel.app/api/step");
check("step: production never http", m.stepUrl(env("http://127.0.0.1:9601/api/step", "off")) === null);
check("step: DEV=on only a local one", m.stepUrl(env("http://127.0.0.1:9601/api/step", "on")) !== null && m.stepUrl(env("https://rigged-outcomes.vercel.app/api/step", "on")) === null);
check("step: no secret, no wake-ups", m.stepUrl(env("https://rigged-outcomes.vercel.app/api/step", "off", "")) === null);

console.log(`${passed}/${passed + failed.length} checks passed (unit)`);
process.exit(failed.length ? 1 : 0);
