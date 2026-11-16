// Rigged Outcomes API: one Cloudflare Worker in front of one D1 database (migrations/0001_init.sql).
// This file routes /api/* to the visitor (public.ts), owner (owner.ts) and job (job.ts, blobs.ts) endpoints,
// answers CORS for the site's own origins only, and turns every failure into
// {"error": code, "message": plain English}: any unexpected throw is 500 "server", with no stack.

import { type Ctx, type Env, HttpError, authorize, json, log, writesOn } from "./lib";
import { commitBlob, deleteBlob, getBlob, getPart, listBlobs, listVersions, putPart } from "./blobs";
import { done, putCatalog, putQueue, work } from "./job";
import { apply, deleteDecision, deleteMarker, overview, putDecision, putSettings } from "./owner";
import { addMarker, addRequest, getMarkers, getRequest, getWatch, undoMarker, unwatch, watch } from "./public";

type Handler = (ctx: Ctx) => Promise<Response>;

const health: Handler = async ({ env }) => json({ ok: true, writes: await writesOn(env) });

/** [method, path, who may call it, handler]; a path's groups become ctx.params. */
const ROUTES: [string, RegExp, "visitor" | "owner" | "job", Handler][] = [
  ["GET", /^\/api\/health$/, "visitor", health],
  ["GET", /^\/api\/markers$/, "visitor", getMarkers],
  ["POST", /^\/api\/markers$/, "visitor", addMarker],
  ["DELETE", /^\/api\/markers\/([^/]+)$/, "visitor", undoMarker],
  ["GET", /^\/api\/watch$/, "visitor", getWatch],
  ["POST", /^\/api\/watch$/, "visitor", watch],
  ["DELETE", /^\/api\/watch$/, "visitor", unwatch],
  ["GET", /^\/api\/request$/, "visitor", getRequest],
  ["POST", /^\/api\/request$/, "visitor", addRequest],
  ["GET", /^\/api\/admin\/overview$/, "owner", overview],
  ["PUT", /^\/api\/admin\/decisions$/, "owner", putDecision],
  ["DELETE", /^\/api\/admin\/decisions$/, "owner", deleteDecision],
  ["DELETE", /^\/api\/admin\/markers\/([^/]+)$/, "owner", deleteMarker],
  ["PUT", /^\/api\/admin\/settings$/, "owner", putSettings],
  ["POST", /^\/api\/admin\/apply$/, "owner", apply],
  ["GET", /^\/api\/job\/work$/, "job", work],
  ["PUT", /^\/api\/job\/queue$/, "job", putQueue],
  ["PUT", /^\/api\/job\/catalog$/, "job", putCatalog],
  ["POST", /^\/api\/job\/done$/, "job", done],
  ["GET", /^\/api\/job\/blobs$/, "job", listBlobs],
  ["GET", /^\/api\/job\/blobs\/([^/]+)$/, "job", getBlob],
  ["DELETE", /^\/api\/job\/blobs\/([^/]+)$/, "job", deleteBlob],
  ["GET", /^\/api\/job\/blobs\/([^/]+)\/versions$/, "job", listVersions],
  ["POST", /^\/api\/job\/blobs\/([^/]+)\/([^/]+)$/, "job", commitBlob],
  ["PUT", /^\/api\/job\/blobs\/([^/]+)\/([^/]+)\/([^/]+)$/, "job", putPart],
  ["GET", /^\/api\/job\/blobs\/([^/]+)\/([^/]+)\/([^/]+)$/, "job", getPart],
];

const METHODS = "GET, POST, PUT, DELETE, OPTIONS";
const HEADERS = "content-type, authorization, x-undo";

/** CORS headers for this request: the origin is echoed only when ALLOWED_ORIGINS lists it. */
function cors(req: Request, env: Env): Record<string, string> {
  const origin = req.headers.get("origin");
  const allowed = (env.ALLOWED_ORIGINS ?? "").split(",").map((s) => s.trim());
  return origin && allowed.includes(origin) ? { "access-control-allow-origin": origin, vary: "Origin" } : { vary: "Origin" };
}

function decode(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

async function route(req: Request, env: Env, url: URL): Promise<Response> {
  const path = url.pathname.replace(/\/+$/, "") || "/";
  let pathMatched = false;
  for (const [method, pattern, who, handler] of ROUTES) {
    const m = pattern.exec(path);
    if (!m) continue;
    pathMatched = true;
    if (method !== req.method) continue;
    if (who !== "visitor") await authorize(req, env, who);
    return handler({ req, env, url, params: m.slice(1).map(decode) });
  }
  if (pathMatched) throw new HttpError(405, "bad_method", `${req.method} is not allowed here.`);
  throw new HttpError(404, "not_found", "No such endpoint.");
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    const headers = cors(req, env);
    if (req.method === "OPTIONS") {
      const preflight = headers["access-control-allow-origin"]
        ? { ...headers, "access-control-allow-methods": METHODS, "access-control-allow-headers": HEADERS, "access-control-max-age": "86400" }
        : headers;
      return new Response(null, { status: 204, headers: preflight });
    }
    let res: Response;
    try {
      res = await route(req, env, url);
    } catch (e) {
      const known = e instanceof HttpError;
      const err = known ? e : new HttpError(500, "server", "Something went wrong on the server.");
      // One line per refusal (never the body, tokens or addresses); unexpected errors keep their message, not the stack.
      log({ event: "error", code: err.code, status: err.status, method: req.method, path: url.pathname, ...(known ? {} : { error: String(e) }) });
      res = json({ error: err.code, message: err.message }, err.status);
    }
    const out = new Response(res.body, res);
    for (const [k, v] of Object.entries(headers)) out.headers.set(k, v);
    return out;
  },
} satisfies ExportedHandler<Env>;
