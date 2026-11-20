// The files the backend publishes, served from D1 (docs/BACKEND.md): GET and HEAD
// /api/data/{index,closed,open}.json and /api/data/markets/<name>.json, from the site:* blobs the step function
// saves (blobs.ts). Each is stored as the gzip of the exact file in base64 parts, so the Worker decodes each part
// and streams the bytes as they are with content-encoding gzip (encodeBody "manual"): it never inflates, parses
// or rebuilds JSON, which keeps a large market page inside the free plan's 10 ms of CPU. The blob's version is
// the ETag, and a matching If-None-Match is a 304 with no body read at all.

import { CONTRACT } from "./contract";
import { type Ctx, PAGE_NAME, fail } from "./lib";

const D = CONTRACT.data;
const LISTS: readonly string[] = D.files;

/** A base64 part as bytes: the runtime's own decoder when it has one, else atob. */
function decode(s: string): Uint8Array {
  const native = (Uint8Array as unknown as { fromBase64?: (t: string) => Uint8Array }).fromBase64;
  if (native) return native.call(Uint8Array, s);
  const bin = atob(s);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/** Whether If-None-Match names this version (weak or strong, or *). */
function matches(header: string | null, version: string): boolean {
  if (!header) return false;
  return header.split(",").some((t) => {
    const tag = t.trim().replace(/^W\//, "");
    return tag === "*" || tag === `"${version}"`;
  });
}

function notFound(): never {
  fail(404, "no_file", "No such file.");
}

export async function data({ req, env, url, params }: Ctx): Promise<Response> {
  const file = params[0];
  const page = file.startsWith(D.market_path) ? /^markets\/([^/]+)\.json$/.exec(file)?.[1] : undefined;
  if (page !== undefined ? !page || !PAGE_NAME.test(page) : !LISTS.includes(file)) notFound();
  const name = `${D.blob_prefix}${file}`;

  const head = await env.DB.prepare("SELECT version FROM job_blobs WHERE name = ?").bind(name).first<{ version: string }>();
  if (!head) notFound();
  const cacheFor = (version: string) =>
    page === undefined ? D.list_cache : url.searchParams.get("v") === version ? D.market_cache_versioned : D.market_cache;
  if (matches(req.headers.get("if-none-match"), head.version)) {
    return new Response(null, { status: 304, headers: { etag: `"${head.version}"`, "cache-control": cacheFor(head.version) } });
  }
  const headers = (version: string): Record<string, string> => ({
    "content-type": "application/json; charset=utf-8",
    "content-encoding": "gzip",
    etag: `"${version}"`,
    "cache-control": cacheFor(version),
  });
  if (req.method === "HEAD") return new Response(null, { headers: headers(head.version), encodeBody: "manual" });

  // the head and the first part together, so a save that lands between the two reads can't mix versions
  const first = await env.DB.prepare(
    `SELECT b.version, b.parts, p.data FROM job_blobs b JOIN job_blob_parts p ON p.name = b.name AND p.version = b.version AND p.part = 0
     WHERE b.name = ?`,
  )
    .bind(name)
    .first<{ version: string; parts: number; data: string }>();
  if (!first) notFound();
  const bytes = decode(first.data);
  if (first.parts === 1) {
    return new Response(bytes, { headers: { ...headers(first.version), "content-length": String(bytes.byteLength) }, encodeBody: "manual" });
  }
  let next = 1;
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(bytes);
    },
    async pull(c) {
      if (next >= first.parts) return c.close();
      const part = await env.DB.prepare("SELECT data FROM job_blob_parts WHERE name = ? AND version = ? AND part = ?")
        .bind(name, first.version, next++)
        .first<string>("data");
      if (part === null) c.error(new Error(`part ${next - 1} of ${name} is gone`));
      else c.enqueue(decode(part));
    },
  });
  return new Response(body, { headers: headers(first.version), encodeBody: "manual" });
}
