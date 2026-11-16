# Deploy

**What gets set up**

- **The site** stays on GitHub Pages.
- **The API** is a Cloudflare Worker: a small program Cloudflare runs for free at an address like `https://rigged-outcomes-api.<your-name>.workers.dev`. The site calls it when a visitor adds a timeline marker, watches an open market or adds a closed one to the main page, and the review page (`#/review`) calls it for your decisions.
- **The database** is Cloudflare D1, a small SQL database attached to the Worker. It stores the markers, the site's one watchlist, the markets added to the main page, your decisions and everything the daily job keeps (its memory between runs, the published lists and the added markets' pages).
- **The bot check** is Cloudflare Turnstile, the "are you a person" check (usually invisible) that runs before any visitor's write is accepted.
- **The job** is a GitHub Action: at 04:17 UTC it makes both Watchlist lists, applies your decisions, adds the markets visitors sent to the main page and republishes the site. It saves to the database only and never commits to the repository.

Everything is on Cloudflare's and GitHub's free plans; Cloudflare doesn't ask for a card.

## Your part: Cloudflare (about 15 minutes)

**1. Create the account**
1. Go to https://dash.cloudflare.com/sign-up, enter your email and a password, and press **Sign up**.
2. Open the email from Cloudflare and click its verification link.
3. Back in the dashboard, skip any "add a domain / what do you want to do" screen. You don't need a domain or a paid plan for any of this.

**2. Check your workers.dev name** (the address the API will live at)
1. In the left menu, click **Workers & Pages** (also under **Compute**, or type "Workers" in the search bar at the top).
2. On that page, look on the right for **Your subdomain**: it shows something like `parthjain.workers.dev`.
   - If it shows a name, you're done with this step. The API will be `https://rigged-outcomes-api.<that name>.workers.dev`.
   - If you want a different name, or there's no name yet, click **Change** (or **Set up**) next to **Your subdomain**, type a name (lowercase letters, numbers and hyphens, e.g. `parthjain`), and confirm.

**3. Create the bot check (Turnstile) widget**
1. In the left menu, click **Turnstile** (or type "Turnstile" in the search bar at the top).
2. Click **Add widget** and fill in:
   - **Widget name:** `Rigged Outcomes`
   - **Hostname management:** click **Add hostnames**, type `parthjain.ca`, add it, and make sure it's ticked.
   - **Widget mode:** **Managed**
   - **Pre-clearance support:** **No**
3. Click **Create**.
4. The next page shows two keys, each with a copy button:
   - **Site Key**: public. It goes into the site so the check can run in visitors' browsers.
   - **Secret Key**: private. It goes into the Worker so it can confirm a visitor passed.

   If you leave this page, go back to **Turnstile**, click **Rigged Outcomes**, and the keys are shown there again.

**4. Put both keys in your `.env` file**
1. Open the file that holds your Gemini key: `Rigged Outcomes/.env`, the folder that contains the repo. For example, in Terminal:
   ```bash
   open -e "/Users/parthjain/Documents/Code/CCT416/Rigged Outcomes/.env"
   ```
2. At the end, add these two lines, each on its own line, with no spaces and no quotes:
   ```
   TURNSTILE_SITEKEY=paste the Site Key here
   TURNSTILE_SECRET=paste the Secret Key here
   ```
3. Save with ⌘S. This file is outside the repo, so it's never committed.

**5. Sign this Mac in to Cloudflare** (so the deploy can create things in your account)
1. In Terminal:
   ```bash
   cd "/Users/parthjain/Documents/Code/CCT416/Rigged Outcomes/Rigged-Outcomes/worker" && npx wrangler login
   ```
2. A browser tab opens saying Wrangler wants access to your account. Click **Allow**. If no tab opens, copy the link Terminal prints into your browser.
3. The tab then says you've granted access. Terminal prints "Successfully logged in".
4. To check, run `npx wrangler whoami`. It should print your email and account name.

Then tell Claude "Cloudflare done".

## Claude's part (done)

1. Created the D1 database `rigged-outcomes` (its id is in `worker/wrangler.toml`) and its tables.
2. Deployed the Worker `rigged-outcomes-api` with its secrets: the Turnstile secret, `OWNER_TOKEN` (your review page password, kept in `.env`), `JOB_TOKEN` and `VISITOR_SALT`. Its address is `WATCHLIST_API_URL` in `.env`.
3. Set the GitHub secrets `GEMINI_API_KEY` and `WATCHLIST_JOB_TOKEN` and the variables `API_URL` and `TURNSTILE_SITEKEY`.
4. Sent the API the catalog of what visitors may write to, and wrote the move of the job's memory into the database (you run it: step 2 below).

**Going live** (yours, in this order, from the repository root unless it says otherwise):

1. The new tables, then the Worker that reads them: `cd worker && npx wrangler d1 migrations apply rigged-outcomes --remote && npx wrangler deploy`. The job running today keeps working.
2. Right after a Watchlist run has finished, with none running or waiting: `git fetch origin watchlist-data && python3 -m watchlist.state migrate`. It copies the branch's state and lists into the database (it reads the API from `.env`), prints the branch commit it copied, and changes nothing in git.
3. Review and commit the code. Before pushing `main`, run `git fetch origin watchlist-data && git rev-parse origin/watchlist-data`. If that commit isn't the one step 2 printed, a Watchlist run (daily, or an apply run from the review page) saved to the branch in between: run `python3 -m watchlist.state migrate --force` again. Then push `main`.
4. Start one run by hand (Actions → Watchlist → Run workflow, mode full) and check that it finishes and that the deploy after it shows both lists.
5. Once a run has succeeded from the database, delete the branch: `git push origin --delete watchlist-data`.

## After

- Open https://parthjain.ca/Rigged-Outcomes/#/review, unlock it with the owner token, and go through the Open now queue.
- Add a marker on a market's Timeline, and press Watch on an Open now card.
- GitHub turns a schedule off after 60 days with no activity in the repository (the daily bot commits used to count as activity; your own commits still do). When the review page warns that no daily run has reported, turn it back on under Actions → Watchlist → **Enable workflow**.

## If something breaks

- **Back to the site without the API:** `gh variable delete API_URL --repo FalconXYX/Rigged-Outcomes && gh workflow run deploy.yml --ref main`. The site then has no Watchlist lists or added markets, and the daily job stops until the variable is back.
- **See the API's errors as they happen:** `cd worker && npx wrangler tail`
- **Stop visitor writes:** the pause switch on `#/review`.
- **Take an added market back off the main page:** `python3 -m watchlist.blobs rm market:<name> && gh workflow run deploy.yml --ref main`. Its request then shows "taken off the main page", and no one can send it again.
- **Undo a bad save of the job's memory or a list:** `python3 -m watchlist.blobs versions state` lists the last 3 saves it replaced, and `python3 -m watchlist.blobs restore state <version>` makes one live again (the same for `list:closed` and `list:open`). Nothing else in the database changes.
- **The database as it was at an earlier time (up to 7 days back, D1 Time Travel; everything since is replaced):** `cd worker && npx wrangler d1 time-travel restore rigged-outcomes --timestamp <ISO time>`
- **The old D3 site back:** GitHub → Settings → Pages → Source: Deploy from a branch → `D3.js`.
- **GitHub job not working:** `scripts/watchlist-mac.sh` runs the same daily job on this Mac.
