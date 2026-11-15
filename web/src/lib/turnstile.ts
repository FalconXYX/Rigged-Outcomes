// The bot check on every visitor write: Cloudflare Turnstile (free). Its
// script loads on the first click that writes, never before, so a visitor who
// only reads never fetches it. The widget is "interaction-only": it stays
// invisible unless Cloudflare wants a click, and then shows in the control's
// slot (a `.ts-slot` span, empty and hidden otherwise). Each token is used
// once and the widget is removed right after.

const SCRIPT = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
/** how long a silent check may take; a check that asks for a click gets INTERACTIVE_MS */
const SILENT_MS = 20_000;
const INTERACTIVE_MS = 120_000;
const LOAD_MS = 15_000;

export type BotAction = "marker" | "watch" | "request";

/** "bot_script": the script could not load (blocked or offline); "bot_check": the check did not pass */
export class BotError extends Error {
  constructor(readonly code: "bot_script" | "bot_check") {
    super(code);
  }
}

type Turnstile = {
  render(el: HTMLElement, opts: Record<string, unknown>): string | undefined;
  execute(id: string): void;
  remove(id: string): void;
};

declare global {
  interface Window {
    turnstile?: Turnstile;
  }
}

let loading: Promise<Turnstile> | null = null;

function load(): Promise<Turnstile> {
  if (window.turnstile) return Promise.resolve(window.turnstile);
  return (loading ??= new Promise<Turnstile>((resolve, reject) => {
    const s = document.createElement("script");
    const fail = () => {
      window.clearTimeout(timer);
      s.remove();
      loading = null; // the next click tries again
      reject(new BotError("bot_script"));
    };
    const timer = window.setTimeout(fail, LOAD_MS);
    s.src = SCRIPT;
    s.async = true;
    s.onload = () => {
      window.clearTimeout(timer);
      if (window.turnstile) resolve(window.turnstile);
      else fail();
    };
    s.onerror = fail;
    document.head.appendChild(s);
  }));
}

/** One run of the widget in `slot`: its token, or a BotError. */
function once(ts: Turnstile, sitekey: string, action: BotAction, slot: HTMLElement): Promise<string> {
  return new Promise((resolve, reject) => {
    const box = document.createElement("div");
    slot.appendChild(box);
    let id: string | undefined;
    let timer = 0;
    let done = false;
    const finish = (token: string | null) => {
      if (done) return;
      done = true;
      window.clearTimeout(timer);
      try {
        if (id) ts.remove(id);
      } catch {
        /* already gone */
      }
      box.remove();
      if (token) resolve(token);
      else reject(new BotError("bot_check"));
    };
    const wait = (msec: number) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => finish(null), msec);
    };
    try {
      id = ts.render(box, {
        sitekey,
        action,
        appearance: "interaction-only",
        execution: "execute",
        "refresh-expired": "manual",
        callback: (token: string) => finish(token),
        "error-callback": () => {
          finish(null);
          return true; // handled: no console error from the widget
        },
        "timeout-callback": () => finish(null),
        "expired-callback": () => finish(null),
        "before-interactive-callback": () => wait(INTERACTIVE_MS),
      });
      if (!id) throw new Error("no widget");
      wait(SILENT_MS);
      ts.execute(id);
    } catch {
      finish(null);
    }
  });
}

/**
 * A fresh token for `action`, shown (if Cloudflare asks for a click) in
 * `slot`, or in a small corner box when the control has none. Tries twice.
 */
export async function botToken(sitekey: string, action: BotAction, slot?: HTMLElement | null): Promise<string> {
  const ts = await load();
  let host = slot;
  let temp: HTMLElement | null = null;
  if (!host || !host.isConnected) {
    temp = document.createElement("div");
    temp.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:60";
    document.body.appendChild(temp);
    host = temp;
  }
  try {
    try {
      return await once(ts, sitekey, action, host);
    } catch {
      return await once(ts, sitekey, action, host);
    }
  } finally {
    temp?.remove();
  }
}
