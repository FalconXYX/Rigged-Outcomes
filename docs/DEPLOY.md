# Deploying the backend and the daily job

This is the runbook for putting the site's backend live: the visitor controls (timeline markers, Watch, "Add to main page"), the private review page, and the daily Watchlist job. Everything here is free and needs no payment card. Nothing in the repository deploys anything by itself; each step below is a command you run or a setting you click.

The first half explains the pieces in plain words. The second half is the list of steps, in order, with a way to check each one.

---

## What each piece is

- **The Worker** (`worker/`, named `rigged-outcomes-api`). A small program that Cloudflare runs for free and that answers the site's requests: "show the markers on this timeline", "add this marker", "how many people watch this market", "ask the owner to add this market". The site itself stays a static page on GitHub Pages; the Worker is the only part that can remember things visitors do.
- **D1** (the database `rigged-outcomes`). The Worker's database, also on Cloudflare's free plan. It holds the markers, the watches, the "Add to main page" requests, your decisions on the review page, the daily job's reports and each day's counters for the daily limits. It never holds an IP address: a visitor is a one-way hash of their address (of its /64 network for an IPv6 address, since one household or phone gets a whole /64). Cloudflare's own per-request logs, which would record addresses, are turned off for this Worker (`worker/wrangler.toml`); its own log lines never carry an address, a hash or a token.
- **Turnstile**. Cloudflare's free bot check. Most visitors never see it; it runs when someone presses a button that writes something, and only asks for a click when it is unsure. The Worker refuses any write without a valid Turnstile answer.
- **The `watchlist-data` branch**. A branch in this repository that only the daily job writes. It is the job's memory between runs (which markets it has already checked, which events entered the Open now pool at what price check, the readings, the cards, your decisions), the two published lists (`closed.json`, `open.json`) that the site deploy copies in, and a daily backup of the D1 database (with no visitor hashes, because the branch is public, and of the visitors' markers only the ones that are still up and at least 2 days old, so a marker you delete within 2 days never reaches the branch). It never mixes with `main`.
- **The daily job** (`.github/workflows/watchlist.yml`). Runs once a day at about 04:17 UTC on GitHub Actions: it reads the branch, fetches your decisions and the approved requests from the Worker, makes the Recently closed and Open now lists, saves everything back to the branch, adds approved markets to the main page (the only commit it ever makes to `main`), reports to the Worker, and redeploys the site. It also has an "apply" mode that only applies your decisions and approved markets, which the review page can start.
- **The review page** (`https://parthjain.ca/Rigged-Outcomes/#/review`). A private page, linked from nowhere, where you paste the owner token. It shows the Open now queue (count a headline as a step, or not), the "Add to main page" requests (approve with a page name, or decline), the markers visitors added (delete any), the watched markets, the last runs, a switch that pauses all visitor changes, and (optionally) a button that starts the job now.
- **The Mac fallback** (`scripts/watchlist-mac.sh`). The same daily job run on this Mac, for days GitHub's runner can't do it (Google News or Polymarket refusing the runner) while GitHub Actions itself still works: it publishes through the deploy workflow. It never adds markets to the main page.

When no API is configured, or the Worker is down, the site behaves exactly as it does today: the buttons say "Not available yet" and nothing else changes.

## What it costs

Nothing. Cloudflare's Workers Free plan, D1's free tier and Turnstile need no card. GitHub Actions is free and unmetered for public repositories on GitHub's standard runners. The daily job uses the free Gemini key that the lists already use, Google News's public RSS search and Polymarket's public API, as the lists do today.

## The GitHub terms "grey area", in full

GitHub's terms for Actions (https://docs.github.com/en/site-policy/github-terms/github-terms-for-additional-products-and-features#actions) ask that Actions be used for the software project in the repository (building, testing, deploying or publishing it), and list uses they don't allow: among them, work unrelated to the repository, crypto mining, serving content like a content delivery network, and anything that puts a burden on GitHub's own servers out of proportion to the benefit to GitHub's users. Calling other websites, which is what the lists do, is not named either way, which is why this is a grey area rather than a clear yes or no. (This is a summary in plain words, not a quote; the link has the exact text.)

How this job sits against that:

- It runs once a day and builds this site's own data: the two lists the site publishes and the markets on its main page. That is the repository's own project.
- It is gentle with the services it calls: about 1,400 Google News searches a day, one a second, from one machine; Polymarket's public API under the rate limits the code already keeps (at most 40% of the documented limits); the Gemini key's free allowance.
- Besides the daily run, it only runs when you start it (the review page's button, or the Actions tab).

What could happen, from likely to rare. The terms say misuse can lead to jobs being stopped, limits on the use of Actions, repositories being disabled, and in some cases the suspension or termination of the GitHub account.

- **Most likely:** nothing; or, if GitHub objected, this repository's runs being stopped or its use of Actions restricted. The Worker keeps answering, and the site stays as it was last deployed. Because the site is deployed by a GitHub Actions workflow, with Actions off the lists freeze: the Mac fallback could still save the job's state, but it can't publish (it starts the deploy workflow).
- **Rare, worst case:** action against the whole `FalconXYX` account, which would also take down the user site at parthjain.ca and every other project on the account.

How the risk is kept small: one run a day; any other run only when you start it; about one news search a second; the Mac fallback for days when Google News or Polymarket refuse GitHub's runner. If Actions were ever turned off for good, the manual way out is to switch Pages to "Deploy from a branch", build `web/` on this Mac with the lists copied into `web/public/data/`, and push the built `web/dist` to a `gh-pages` branch; that is not set up here.

## Free-tier limits that shape the design

| Limit | What it means here |
|---|---|
| Workers: 100,000 requests a day; 10 ms of CPU per request; 50 subrequests | About one request per market page and one per Watchlist sheet opened. Past the limit Cloudflare answers with error 1027 and the site shows "Not available yet" until 00:00 UTC. |
| D1: 5 million rows read and 100,000 rows written a day; 100 KB per statement; 100 bound parameters; 2 MB per row | The worst day under the visitor limits writes about 4,000 rows. The review queue is stored one row per item to stay far under 100 KB. |
| Turnstile: free, no card; each answer works once and for 300 seconds | One widget, for the hostname `parthjain.ca`. |
| GitHub Actions (public repository): free; 6 hours per job; cache 10 GB with entries removed after 7 days unused; schedules run only on the default branch and are turned off after 60 days with no activity in the repository; a push made with the job's own token starts no other workflow | The schedule is at minute 17 (GitHub delays on-the-hour schedules). The job calls the deploy itself, because its own push to `main` would not start it. The review page warns when no run has reported for 30 hours. |
| Git: GitHub warns at 50 MB per file and refuses 100 MB | A market's clean CSV must be 45 MB or less to be added automatically. Pruning keeps the state files small (about 8 MB today). |

One accepted risk of the free plan: anyone can spend the Worker's 100,000 requests in a day (for example a script asking `/api/watch` about 70 times a minute). Every visitor control then reads "Not available yet" until 00:00 UTC; nothing else breaks. If parthjain.ca's DNS ever moves to Cloudflare, the Worker can run on the custom domain and get the free plan's rate-limiting rule.

## When something fails

| Failure | What a visitor sees | What the job does | How it recovers |
|---|---|---|---|
| No API configured, the Worker down, error 1027, or D1 over its daily limit | "Not available yet"; timelines keep their `markets.json` markers; the Watchlist itself is unaffected | `sync` warns and the lists still run; decisions and requests wait | On its own; limits reset at 00:00 UTC |
| You paused visitors' changes | "Not available right now" | Unaffected | Allow them again on the review page |
| Turnstile blocked or failing | The bot-check words; one automatic retry | – | – |
| Gemini down or out of allowance | "Not updated today" on the Open now list (as today) | The run is "partial"; it still publishes what it has | The next run |
| Google News refuses GitHub's runner | The last Open now list stays, marked "not updated today" | After 20 failed searches in a row it stops searching; the run is "partial" | The Mac fallback |
| Polymarket refuses GitHub's runner | The previous lists stay | Those steps fail; the state is still saved and the unchanged lists are published | The Mac fallback |
| The `watchlist-data` branch missing or damaged | The last deploy stays | Stops before changing anything; no push, no deploy | Missing: seed it (step 8). Damaged: on this Mac, `python3 -m watchlist.state pull --rev <a good sha> --force`, then `python3 -m watchlist.state push --onto-head --by mac`, which puts that state back on top of the branch (the history stays) |
| The branch moved during a run (the Mac and GitHub overlapped) | – | The push is refused, loudly; decisions stay pending | The next run starts from the new head |
| Adding a market fails, is too big, or its step runs out of time | "Approved, but adding it failed…" | `markets.json` is put back, the reason is stored ("stopped part way…" when the step was stopped) | Fix the cause, then "Try again" on the review page, or decline it |
| The push to `main` conflicts | The request still reads "Approved" | One rebase and retry, then the step fails; "added" is never reported | It is retried by the next run, harmlessly |
| The schedule turned off after 60 quiet days | The lists age ("Last updated …") | No runs | The review page warns after 30 hours; turn it back on in the Actions tab (Watchlist → Enable workflow) |
| "Apply decisions now" pressed while the daily run waits to start (an earlier apply run still going at 04:17 UTC) | – | GitHub keeps one waiting run per group, so the newer apply run replaces the waiting daily run: that day's lists don't run | Don't press it twice between about 04:00 and 07:00 UTC; if it happened, `gh workflow run watchlist.yml -f mode=full` |
| Spam | The daily limits hold (10 markers per visitor and 20 new ones per timeline a day; one IPv6 /64 counts as one visitor) | – | Delete markers on the review page; pause visitors' changes |

---

## The steps

Run these in order, from Terminal. Paths assume the repository at `/Users/parthjain/Documents/Code/CCT416/Rigged Outcomes/Rigged-Outcomes`, with the `.env` file (the Gemini key) next to it in `Rigged Outcomes/`. `gh` must be signed in (`gh auth status`).

### 1. Review and commit tonight's work

On the `Refresh` branch, look over the changes and commit them yourself. Before committing, check that everything still passes:

```bash
cd "/Users/parthjain/Documents/Code/CCT416/Rigged Outcomes/Rigged-Outcomes"
for f in tests/test_*.py; do python3 "$f" > /dev/null || echo "FAILED: $f"; done
npm --prefix web run typecheck
cd worker && npm install && npm run typecheck && npm test && cd ..
```

`npm test` in `worker/` starts a local Worker on port 8788 with a throwaway local database and runs every endpoint's check; it never contacts your Cloudflare account. (`tests/test_v2_parity.py` uses the network and takes 2 to 3 minutes.)

**Check:** no "FAILED" line; both type-checks end without an error (npm prints only its `> … typecheck` header lines); `npm test` ends with every check passed.

### 2. Sign in to Cloudflare from the Worker's folder

```bash
cd "/Users/parthjain/Documents/Code/CCT416/Rigged Outcomes/Rigged-Outcomes/worker"
npm install
npx wrangler login
```

A browser window opens; sign in to Cloudflare (create a free account if you have none; no card is asked for) and allow Wrangler.

**Check:** `npx wrangler whoami` prints your account's email and id.

### 3. Create the database

```bash
npx wrangler d1 create rigged-outcomes
```

It prints a block of settings with `database_id = "…"`. Copy only the `database_id` value: open `worker/wrangler.toml` and replace the placeholder `00000000-0000-0000-0000-000000000000` with it. Keep `binding = "DB"` as it is (the printed block names the binding differently; the code uses `DB`). Then create the tables:

```bash
npx wrangler d1 migrations apply rigged-outcomes --remote
```

**Check:** `npx wrangler d1 execute rigged-outcomes --remote --command "SELECT key, value FROM settings"` lists `writes | on` and `last_dispatch | 0`.

### 4. Create the Turnstile widget

In the Cloudflare dashboard (https://dash.cloudflare.com): **Turnstile** in the left menu (under "Protect & Connect", or search for it) → **Add widget**.

- Widget name: `Rigged Outcomes`
- Hostname management: **Add hostnames** → `parthjain.ca`
- Widget mode: **Managed**
- Pre-clearance: **No**

Press **Create**. Keep the page open: it shows the **Site Key** (public, it goes into the site) and the **Secret Key** (private, it goes into the Worker).

**Check:** the widget is listed under Turnstile with the hostname `parthjain.ca`.

### 5. Give the Worker its secrets

Still in `worker/`. None of these print a secret on screen. The Worker doesn't exist yet, so the first command asks whether to create it ("There doesn't seem to be a Worker called rigged-outcomes-api…"): answer **y**. The others then find it.

```bash
# the Turnstile secret: answer y to creating the Worker, then paste the Secret Key from step 4 when asked
npx wrangler secret put TURNSTILE_SECRET

# your owner token for the review page: made here, sent to the Worker and put on your clipboard
O=$(openssl rand -hex 32) && printf %s "$O" | npx wrangler secret put OWNER_TOKEN && printf %s "$O" | pbcopy; unset O
```

Now paste the clipboard into your password manager as "Rigged Outcomes owner token". It is the only copy.

The `.env` file next to the repository ends without a line break after the Gemini key, so the line below adds one first; without it the new line would be glued onto the key and break it.

```bash
# the job's token: the same value goes to the Worker, to GitHub, and to the .env file next to the repository
# (each part runs only if the one before it worked)
T=$(openssl rand -hex 32) && printf %s "$T" | npx wrangler secret put JOB_TOKEN && printf %s "$T" | gh secret set WATCHLIST_JOB_TOKEN --repo FalconXYX/Rigged-Outcomes && { [ -z "$(tail -c1 ../../.env)" ] || echo >> ../../.env; } && printf 'WATCHLIST_JOB_TOKEN=%s\n' "$T" >> ../../.env; unset T

# the salt that turns an IP address into a visitor hash
printf %s "$(openssl rand -hex 32)" | npx wrangler secret put VISITOR_SALT
```

Check that the `.env` file still reads right (this prints the key's length and two yes/no answers, never a value):

```bash
(cd .. && python3 -c 'from watchlist import ai, sync; k = ai.api_key(); print(len(k), "WATCHLIST" in k, bool(sync._env("WATCHLIST_JOB_TOKEN")))')
```

It should print the key's usual length (39 for a Gemini key), then `False True`. A `True` in the middle means the token was glued onto the key: open `Rigged Outcomes/.env` in a text editor and put `WATCHLIST_JOB_TOKEN=…` on its own line.

Optional (the review page's **Apply decisions now** button; without it, decisions and approvals wait for the 04:17 UTC run): create a fine-grained personal access token on GitHub. Your avatar → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**. Name it `rigged-outcomes-dispatch`; Resource owner `FalconXYX`; Expiration up to one year; Repository access **Only select repositories** → `Rigged-Outcomes`; Repository permissions → **Actions: Read and write** (Metadata: Read-only is added by itself). Generate it, copy it, then:

```bash
npx wrangler secret put GITHUB_DISPATCH_TOKEN
```

**Check:** `npx wrangler secret list` shows `JOB_TOKEN`, `OWNER_TOKEN`, `TURNSTILE_SECRET`, `VISITOR_SALT` (and `GITHUB_DISPATCH_TOKEN` if you made it); `gh secret list --repo FalconXYX/Rigged-Outcomes` shows `WATCHLIST_JOB_TOKEN`.

### 6. Deploy the Worker

```bash
npx wrangler deploy
```

The first time, Wrangler asks you to pick a `workers.dev` subdomain (or shows a link to register one in the dashboard: **Workers & Pages** → your subdomain). It prints the Worker's address, `https://rigged-outcomes-api.<your subdomain>.workers.dev`. Keep it in a shell variable for the next steps, in this same Terminal window (replace `YOURSUB`; the quotes matter, and a leftover `<` would make the shell read a file):

```bash
API='https://rigged-outcomes-api.YOURSUB.workers.dev'
```

**Check:**

```bash
curl -s "$API/api/health"
# {"ok":true,"writes":true}
curl -s -o /dev/null -w "%{http_code}\n" "$API/api/admin/overview"
# 401  (the owner endpoints need the token)
```

### 7. Tell GitHub and the Mac where the API is

From the repository root:

```bash
cd "/Users/parthjain/Documents/Code/CCT416/Rigged Outcomes/Rigged-Outcomes"

# the API's address for this Mac's runs, on its own line in ../.env
{ [ -z "$(tail -c1 ../.env)" ] || echo >> ../.env; } && printf 'WATCHLIST_API_URL=%s\n' "$API" >> ../.env

# the key must read right before it goes to GitHub: its length, then False (prints no value)
python3 -c 'from watchlist import ai; k = ai.api_key(); print(len(k), "WATCHLIST" in k)'

# the Gemini key, from the .env file to GitHub, never shown on screen
python3 -c 'from watchlist import ai; print(ai.api_key(), end="")' | gh secret set GEMINI_API_KEY --repo FalconXYX/Rigged-Outcomes

gh variable set API_URL --repo FalconXYX/Rigged-Outcomes --body "$API"
gh variable set TURNSTILE_SITEKEY --repo FalconXYX/Rigged-Outcomes --body 'the Site Key from step 4'
```

(Paste the Site Key in place of `the Site Key from step 4`, inside the quotes.) `API_URL` and `TURNSTILE_SITEKEY` are build settings, not secrets: the site's code needs both to show the controls. With neither set, the deploy builds today's site exactly.

**Check:** `gh secret list --repo FalconXYX/Rigged-Outcomes` shows `GEMINI_API_KEY` and `WATCHLIST_JOB_TOKEN`; `gh variable list --repo FalconXYX/Rigged-Outcomes` shows `API_URL` and `TURNSTILE_SITEKEY`; `python3 -c 'from watchlist import sync; print(sync.client())'` prints `Client('https://rigged-outcomes-api.YOURSUB.workers.dev')` (the token is never printed).

### 8. Seed the job's memory and the catalog

This creates the `watchlist-data` branch on GitHub from this Mac's store and the current lists (including tonight's Recently closed rerun and the Nov 13 approvals), then tells the Worker what visitors may write to:

```bash
python3 -m watchlist.state push --seed --by mac
python3 -m watchlist.sync catalog
```

The first command builds one commit with git's plumbing and pushes it; it does not switch branches or touch your working tree. The second prints the catalog's counts.

**Check:** `git ls-remote origin watchlist-data` prints one line; `git show origin/watchlist-data:state/manifest.json` (after `git fetch origin watchlist-data`) shows the tables and their row counts; the catalog line shows `'market': 7` and non-zero `closed` and `open` counts.

### 9. Switch GitHub Pages to the new site

This is the open decision from before: it replaces the older D3 story site at `parthjain.ca/Rigged-Outcomes/` (served from the `D3.js` branch). If that site needs to stay up, keep a copy first.

- Repository → **Settings** → **Pages** → Build and deployment → Source: **GitHub Actions**, and tick **Enforce HTTPS** on the same page. Or, both at once: `gh api -X PUT repos/FalconXYX/Rigged-Outcomes/pages -f build_type=workflow -F https_enforced=true`. HTTPS matters here: today `http://parthjain.ca/Rigged-Outcomes/` answers without redirecting, and the Worker only answers pages from `https://parthjain.ca`, so a visitor on the plain `http://` address would see "Not available yet" everywhere and the review page couldn't load.
- Repository → **Settings** → **Environments** → `github-pages` → Deployment branches and tags → **Add deployment branch or tag rule** → `main`. Or: `gh api -X POST repos/FalconXYX/Rigged-Outcomes/environments/github-pages/deployment-branch-policies -f name=main`

**Check:**

```bash
gh api repos/FalconXYX/Rigged-Outcomes/pages --jq '.build_type, .https_enforced'
# workflow
# true
gh api repos/FalconXYX/Rigged-Outcomes/environments/github-pages/deployment-branch-policies --jq '.branch_policies[].name'
# main is listed (D3.js may be too)
curl -sI http://parthjain.ca/Rigged-Outcomes/ | head -1
# HTTP/1.1 301 Moved Permanently
```

### 10. Put the work on `main`

```bash
git checkout main && git merge --ff-only Refresh && git push origin main && git checkout Refresh
```

The push starts "Deploy dashboard to GitHub Pages". It now copies the lists from `watchlist-data` and builds with `API_URL` and `TURNSTILE_SITEKEY`.

**Check:** `gh run watch` (pick the deploy run) ends green; https://parthjain.ca/Rigged-Outcomes/#/watchlist shows both lists; opening a card's details shows a Watch button (an open card) or "+ Add to main page" (a closed card) instead of "Not available yet"; a market's timeline shows the Events control.

Then two real writes, because only the live site can test the real Turnstile secret, its hostname and action check, and CORS from parthjain.ca:

1. On https://parthjain.ca/Rigged-Outcomes/#/m/usiran, open **Events**, add a marker labelled `test` at any time inside the chart, reload the page, check it is still there, then press **Remove** on it.
2. On the Watchlist, open an Open now card's details, press **Watch this market**, then **Stop watching**.

If either says "The bot check didn't pass" or "Couldn't send right now": in another Terminal window, `cd worker && npx wrangler tail`, try again, and read the line it prints. `bot_check_failed` usually means the Site Key was pasted where the Secret Key belongs (step 5, `TURNSTILE_SECRET`); `bot_check_mismatch` means the widget's hostname isn't `parthjain.ca` (step 4); an `error` line names the code.

### 11. Run the daily job once by hand

```bash
gh workflow run watchlist.yml -f mode=full && sleep 5 && gh run watch
```

It takes one to two hours (most of it the Open now list's model readings). It deploys the site when it finishes.

**Check:** the run ends green. (When a list fails, the run ends red with that step marked, the other steps still save and publish what they have, and the summary's "Watchlist job" paragraph says what was updated.) Its summary page has the "Watchlist job" paragraph; `git fetch origin watchlist-data && git log --oneline -2 origin/watchlist-data` shows a new "State and lists, …" commit.

### 12. Open the review page

Go to https://parthjain.ca/Rigged-Outcomes/#/review, paste the owner token from your password manager, and press **Unlock**. The page keeps the token in its own memory only (never in the browser's storage, which every GitHub Pages site on parthjain.ca shares), so reloading the page asks for it again.

**Check:** the Status line shows the run from step 11; the Open now queue for the latest day is listed; "Visitors' changes: allowed".

### 13. If something goes wrong: rolling back

- **The Worker:** `cd worker && npx wrangler rollback` returns to the previous version (`npx wrangler deployments list` shows them).
- **Stop visitors' changes at once:** the pause switch on the review page ("Pause visitors' changes"). Reading still works; undo and unwatch keep working.
- **Stop the daily job:** `gh workflow disable watchlist.yml` (`gh workflow enable watchlist.yml` to resume).
- **Back to today's site exactly:** `gh variable delete API_URL --repo FalconXYX/Rigged-Outcomes && gh workflow run deploy.yml --ref main`. With no API in the build, the site makes no API requests at all.
- **The database:** D1 keeps 7 days of point-in-time history on the free plan (Time Travel): `npx wrangler d1 time-travel info rigged-outcomes`, then `npx wrangler d1 time-travel restore rigged-outcomes --timestamp <ISO time>`. Beyond that, `backup/d1.json` on the `watchlist-data` branch has every run's export (everything but visitor hashes, and of the markers only those that were up and at least 2 days old); restoring from it is manual SQL.
- **The job's memory:** every run is a commit on `watchlist-data` (`git log --oneline origin/watchlist-data` after `git fetch origin watchlist-data`). To go back to an older one: `python3 -m watchlist.state pull --rev <the older sha> --force` loads it into this Mac's store, then `python3 -m watchlist.state push --onto-head --by mac` puts it back on top of the branch (a plain `push` refuses, because the branch's head isn't what was pulled). Do it outside about 04:00–07:00 UTC, or with the job off (`gh workflow disable watchlist.yml`), so the daily run doesn't push in between.

### 14. Maintenance

- **The branch is the source of truth after go-live.** This Mac's store is a copy. Before working on the lists locally, `python3 -m watchlist.state pull`. It refuses to replace a local store that holds something newer than the branch (a list day, a decision or a closed check made here since); `--force` replaces it (the old store is backed up to `data/watchlist/watchlist.db.bak-<time>`, the newest 3 kept). Local runs never push unless you run `python3 -m watchlist.state push`, which refuses when the branch moved since your pull.
- **The Mac fallback:** `scripts/watchlist-mac.sh` runs the whole daily job here (under `caffeinate`, logging to `data/watchlist/logs/`), pushes the state and starts the deploy. It refuses to start while the GitHub job is running or waiting to run. It never adds markets; it says how many approved requests wait.
- **Undoing an applied decision:** on the review page, under "Applied by recent runs", press the opposite button ("Mark as not a step", "Count as a step", "Don't show it" or "Show this card"); the next run applies it. "Undo" on a decision still waiting also reaches a run that had already fetched it: that run may show it for a day, and the next one takes it out. From the command line instead: `python3 -m watchlist.state pull`, `python3 -m watchlist.opennow --review`, `python3 -m watchlist.opennow --reject N.M` (or `--approve N.M`), `python3 -m watchlist.state push --by mac`, then `gh workflow run deploy.yml --ref main`; do it outside about 04:00–07:00 UTC so the push isn't refused.
- **Running the Mac fallback every day (optional):** save this as `~/Library/LaunchAgents/ca.parthjain.rigged-watchlist.plist`, then `launchctl load ~/Library/LaunchAgents/ca.parthjain.rigged-watchlist.plist`. `StartCalendarInterval` runs a missed run when the Mac wakes. Use it only while the GitHub job is off (`gh workflow disable watchlist.yml`), so the two don't race. launchd starts jobs with a bare `PATH` (where `python3` is the system's Python 3.9 without `requests`, and `gh` isn't found), so the plist sets one.

  ```xml
  <?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0">
  <dict>
    <key>Label</key><string>ca.parthjain.rigged-watchlist</string>
    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>/Users/parthjain/Documents/Code/CCT416/Rigged Outcomes/Rigged-Outcomes/scripts/watchlist-mac.sh</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
      <key>PATH</key><string>/Library/Frameworks/Python.framework/Versions/3.12/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>23</integer><key>Minute</key><integer>17</integer></dict>
    <key>StandardOutPath</key><string>/tmp/rigged-watchlist.out</string>
    <key>StandardErrorPath</key><string>/tmp/rigged-watchlist.err</string>
  </dict>
  </plist>
  ```

  (23:17 local time in Toronto is 04:17 UTC in winter.) Test it once right away: `launchctl kickstart -k gui/$(id -u)/ca.parthjain.rigged-watchlist`, then read `/tmp/rigged-watchlist.out` and `/tmp/rigged-watchlist.err`. If the error file says "Operation not permitted", macOS is keeping the background job out of `~/Documents`: allow it under System Settings → Privacy & Security (Full Disk Access for `/bin/bash`, or Files and Folders), or keep running the script by hand. `launchctl unload` the same path to stop it.
- **A yearly squash of `watchlist-data`:** the branch gains one commit a day. Once a year, while the job is off (`gh workflow disable watchlist.yml`): `python3 -m watchlist.state pull --force`, then `git push origin --delete watchlist-data`, then `python3 -m watchlist.state push --seed --by mac`, then `gh workflow enable watchlist.yml`. The history goes; the current state stays.
- **Renew the dispatch token** before it expires (GitHub emails a week ahead): make a new one as in step 5 and `npx wrangler secret put GITHUB_DISPATCH_TOKEN` again.
- **After adding a market to `markets.json` by hand,** the Worker learns about it (so visitors can add markers to it) at the next daily run, or at once with `python3 -m watchlist.sync catalog`.
- **Pull before you push to `main`:** an approved market is committed to `main` by the job, so `git pull` before your next push.

---

## How the pieces talk to each other

```
visitor's browser ──GET / POST with a Turnstile answer──> Worker ──> D1
review page ──owner token──> Worker ──(optional dispatch token)──> GitHub: start watchlist.yml in apply mode
watchlist.yml ──job token──> Worker: GET /api/job/work, PUT /api/job/queue, PUT /api/job/catalog,
                                     POST /api/job/done, GET /api/job/export
watchlist.yml <──git──> watchlist-data: state/*.jsonl, state/manifest.json, site/{closed,open}.json, backup/d1.json
watchlist.yml ──git push──> main (only approved markets: markets.json + data/<Output>_clean.csv + data/<Output>_insiders.csv)
watchlist.yml ──calls──> deploy.yml: export.py, the lists from watchlist-data, the Vite build, Pages
```

One daily run, step by step (`full` mode; `apply` skips the two lists):

1. `python3 -m watchlist.state pull`: the branch's state into a fresh store, the lists into `web/public/data/`. It stops the run when the branch is missing or does not match its manifest.
2. The news searches cache from the Actions cache (it only spares a same-day rebuild its searches).
3. `python3 -m watchlist.sync pull`: your pending decisions, the approved requests and the watch counts, into `data/watchlist/work.json`.
4. `python3 -m watchlist.sync decide --mode full`: your decisions into the store (in `apply` mode the reviewed day is rebuilt at once, like `--approve`).
5. `python3 -m watchlist.upsets --days 2 --watched data/watchlist/work.json`: Recently closed, yesterday and today so far, and any older kept day that a failed run left counted only in part (each day records when it was counted, and the site says "by 04:17" on a partial one); a pick whose open card visitors watch says how many.
6. `python3 -m watchlist.opennow`: Open now; it reads your decisions.
7. `python3 -m watchlist.state push`: prunes, then commits the state, the lists and the API's backup to `watchlist-data` (refused if the branch moved since step 1).
8. `python3 -m watchlist.sync add`, then a commit to `main` of each added market's `markets.json` entry and two CSVs.
9. `python3 -m watchlist.sync push`: the new review queue and catalog to the Worker, then the report. A decision counts as applied only if step 7 succeeded in this run; a market counts as added only once step 8's push to `main` succeeded.
10. The deploy: `deploy.yml` builds `main` with the new lists.

The job's files in `data/watchlist/` (gitignored): `work.json`, `decide_results.json`, `add_results.json`, `state_pulled.json`, `state_pushed.json`, `markets_pushed.ok`. Code: `watchlist/state.py`, `watchlist/sync.py`, `add_market.py`; tests: `tests/test_watchlist_state.py`, `tests/test_watchlist_sync.py`, `tests/test_add_market.py`.
