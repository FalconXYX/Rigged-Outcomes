// The code a stack runs: one snapshot of the repository's working tree, taken when the stack starts (what
// `git ls-files` lists, tracked or new, so uncommitted changes are in and ignored files are not: no .env, no
// .dev.vars, no data/watchlist/, no web/public/data/, exactly what a checkout of main would hold if this tree
// were pushed). The Worker, the step server, the site's build and each command a scenario runs get a copy of it
// as their workspace. Nothing is written into the repository itself; an edit made in the repository while a
// stack runs is not picked up by it.
//
// node_modules are not copied: each copy's web/node_modules and worker/node_modules is a folder of links to
// the repository's installed packages (`npm ci` is left out), so caches a build writes
// there (node_modules/.vite and the like) land in the copy, never in the repository's own node_modules.

import { execFileSync } from "node:child_process";
import { constants, cpSync, existsSync, mkdirSync, readdirSync, rmSync, symlinkSync } from "node:fs";
import { dirname, join } from "node:path";

/** The folders whose node_modules are linked into a copy. */
const PACKAGES = ["web", "worker"];
/** What a build or a tool writes inside node_modules: kept out of the links, so it is written in the copy. */
const WRITABLE = new Set([".vite", ".vite-temp", ".cache", ".tmp"]);

/** Copy the working tree of `repo` into `dest` (emptied first); returns the number of files. */
export function snapshot(repo, dest) {
  rmSync(dest, { recursive: true, force: true });
  mkdirSync(dest, { recursive: true });
  const list = execFileSync("git", ["-C", repo, "ls-files", "-co", "--exclude-standard", "-z"], { maxBuffer: 64 << 20 })
    .toString("utf8")
    .split("\0")
    .filter(Boolean);
  let n = 0;
  for (const rel of list) {
    const from = join(repo, rel);
    if (!existsSync(from)) continue; // tracked but deleted in the working tree
    mkdirSync(dirname(join(dest, rel)), { recursive: true });
    cpSync(from, join(dest, rel), { mode: constants.COPYFILE_FICLONE, preserveTimestamps: true });
    n++;
  }
  return n;
}

/** Fill `dest` with a copy of `snap` (a clone on APFS, so it costs next to nothing) and link its packages from `repo`. */
export function workspace(snap, dest, repo) {
  rmSync(dest, { recursive: true, force: true });
  mkdirSync(dirname(dest), { recursive: true });
  cpSync(snap, dest, { recursive: true, mode: constants.COPYFILE_FICLONE, preserveTimestamps: true });
  linkPackages(dest, repo);
  return dest;
}

/** web/node_modules and worker/node_modules in `dest` as folders of links to `repo`'s installed packages. */
export function linkPackages(dest, repo) {
  for (const pkg of PACKAGES) {
    const real = join(repo, pkg, "node_modules");
    if (!existsSync(real) || !existsSync(join(dest, pkg))) continue;
    const mine = join(dest, pkg, "node_modules");
    rmSync(mine, { recursive: true, force: true });
    mkdirSync(mine);
    for (const entry of readdirSync(real)) {
      if (WRITABLE.has(entry)) continue;
      symlinkSync(join(real, entry), join(mine, entry));
    }
  }
}

/** Whether `repo` has the packages the stack needs installed (npm install in web/ and worker/). */
export function missingPackages(repo) {
  const need = [
    ["web", join("web", "node_modules", "vite")],
    ["worker", join("worker", "node_modules", "wrangler")],
  ];
  return need.filter(([, p]) => !existsSync(join(repo, p))).map(([pkg]) => pkg);
}
