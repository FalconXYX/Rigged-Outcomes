// The stack's GitHub Pages: the last deployed build of web/ served at http://127.0.0.1:<port>/Rigged-Outcomes/
// (the production path, so the relative base is tested as it runs), with Pages' own caching (max-age=600, an
// ETag, 304 for a matching If-None-Match), its 404 page for a missing file and a redirect from / to the site.
// A deploy (the stack's one build, as deploy.yml makes it) copies the build into a folder of its own and switches
// to it at once, so a reader never sees half a deploy; until the first one the site answers 404.

import { createHash } from "node:crypto";
import { cpSync, createReadStream, existsSync, mkdirSync, rmSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize } from "node:path";

const BASE = "/Rigged-Outcomes/";
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".woff2": "font/woff2",
  ".woff": "font/woff",
  ".txt": "text/plain; charset=utf-8",
  ".map": "application/json; charset=utf-8",
};

export class Site {
  constructor({ port, dir }) {
    this.port = port;
    this.dir = dir;
    this.url = `http://127.0.0.1:${port}${BASE}`;
    this.origin = `http://127.0.0.1:${port}`;
    this.current = null;
    this.deploys = [];
  }

  /** Publish a build folder; returns the site's URL. */
  async deploy(from, label) {
    const n = this.deploys.length + 1;
    const to = join(this.dir, `deploy-${n}`);
    rmSync(to, { recursive: true, force: true });
    mkdirSync(this.dir, { recursive: true });
    cpSync(from, to, { recursive: true });
    this.current = to;
    this.deploys.push({ n, label, at: Date.now(), dir: to });
    return this.url;
  }

  handle(req, res) {
    const url = new URL(req.url, this.origin);
    if (url.pathname === "/" || url.pathname === BASE.slice(0, -1)) return res.writeHead(302, { location: BASE }).end();
    const notFound = () =>
      res.writeHead(404, { "content-type": "text/html; charset=utf-8" }).end(req.method === "HEAD" ? undefined : "<!doctype html><title>Site not found · GitHub Pages</title><h1>404</h1>");
    if (!url.pathname.startsWith(BASE) || !this.current) return notFound();
    let rel = decodeURIComponent(url.pathname.slice(BASE.length));
    if (rel === "" || rel.endsWith("/")) rel += "index.html";
    const file = normalize(join(this.current, rel));
    if (!file.startsWith(this.current) || !existsSync(file) || !statSync(file).isFile()) return notFound();
    const st = statSync(file);
    const etag = `"${createHash("sha1").update(`${this.current}:${rel}:${st.size}:${st.mtimeMs}`).digest("hex").slice(0, 16)}"`;
    const headers = {
      "content-type": TYPES[extname(file)] ?? "application/octet-stream",
      "cache-control": "max-age=600",
      etag,
      "last-modified": st.mtime.toUTCString(),
      "access-control-allow-origin": "*",
    };
    if (req.headers["if-none-match"] === etag) return res.writeHead(304, headers).end();
    res.writeHead(200, { ...headers, "content-length": st.size });
    if (req.method === "HEAD") return res.end();
    createReadStream(file).pipe(res);
  }

  listen() {
    this.server = createServer((req, res) => this.handle(req, res));
    return new Promise((resolve, reject) => {
      this.server.once("error", reject);
      this.server.listen(this.port, "127.0.0.1", () => resolve(this));
    });
  }

  close() {
    return new Promise((r) => {
      if (!this.server) return r();
      this.server.closeAllConnections?.();
      this.server.close(() => r());
    });
  }
}
