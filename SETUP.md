## Prerequisites

- A Monzo account with developer access enabled at [developers.monzo.com](https://developers.monzo.com)
- A server with a public IP address and Docker installed (e.g. Oracle Cloud free tier)
- A Postgres database for it — this setup uses a second free-tier box running the
  [data-vault](https://github.com/your-username/data-vault) repo, which also backs it up
  nightly and runs the WireGuard VPN your local machine reaches it through. Its
  `new_project.sh` makes this project's schema and login (`DB_*` below), and its
  `wg_add_device.sh` makes your local machine's VPN config.
- A free [DuckDNS](https://www.duckdns.org) subdomain pointing at your server IP (required for HTTPS)
- Python 3.11+ and [Poetry](https://python-poetry.org/docs/#installation) installed on your local machine
- Git installed on both your local machine and the server

---

## Part 1 — Server Setup

### 1. Install Docker

SSH into your server and run:

```bash
curl -fsSL https://get.docker.com | sh
```

### 2. Clone the Repository

```bash
git clone git@github.com:your-username/bank-enrichment.git
cd bank-enrichment
```

**On a small instance (e.g. Oracle's free 1GB-RAM AMD Micro), add a swapfile too.** With no swap, any brief memory spike — a burst of webhooks, a dependency update, Caddy renewing a certificate — gets a container killed outright mid-request. Swap turns that into a moment of slowness instead. From the repo root:

```bash
sudo ./setup_swap.sh        # 2G by default; pass a size to change it, e.g. sudo ./setup_swap.sh 1G
```

It creates `/swapfile`, adds it to `/etc/fstab` so it survives reboots, and sets `vm.swappiness=10` so the system only swaps under real memory pressure rather than leaning on it day to day. It's safe to re-run — anything already in place is left alone. Check with `free -h`: the `Swap:` line should show the size you chose.

### 3. Create a Telegram Bot

The system sends push notifications via a Telegram bot when a transaction arrives.

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts to choose a name and username
3. BotFather will give you a token like `123456789:ABCdef...` — this is your `TELEGRAM_API` key

**Get your chat ID:**

1. Search for your bot in Telegram and send it any message
2. Visit this URL in your browser (replace with your token):

```
https://api.telegram.org/bot{YOUR_TOKEN}/getUpdates
```

3. Find `result[0].message.chat.id` in the response — that number is your `TELEGRAM_CHAT_ID`

### 4. Configure Environment Variables

`config/.env` is a single file that is used by both the server (loaded by Docker Compose) and your local machine (loaded automatically by the processing script). Fill it in once locally, then copy it to the server.

On your **local machine**, from the project root:

```bash
cp config/.env.example config/.env
```

Then open `config/.env` and fill in all values:

| Key | Used by | Description |
|---|---|---|
| `DASHBOARD_USER` | Server | Username for the dashboard login |
| `DASHBOARD_PASSWORD` | Server | Password for the dashboard login |
| `TELEGRAM_API` | Both | Your Telegram bot token from @BotFather — the pipeline sends its summaries and alerts with it too |
| `TELEGRAM_CHAT_ID` | Both | Your personal Telegram chat ID (see step 3) |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Both | The database login, which data-vault's `new_project.sh` prints and saves on the data box. `DB_HOST` is the data box's private address, which the server reaches inside the cloud network and your local machine through the VPN |
| `BACKUP_DIR` | Local (optional) | Where the dashboard's **Back up now** saves its copies — default `~/bank-enrichment-backups` |
| `LOG_DIR` | Local (optional) | Where the pipeline's log files go — default `logs/` in the repo |
| `CLAUDE_SECRET` | Local | Your Anthropic API key from [console.anthropic.com](https://console.anthropic.com) — used by the LLM classifier |
| `MONZO_CLIENT_ID` | Server | OAuth client ID from a **confidential** client registered at [developers.monzo.com](https://developers.monzo.com) — used to silently refresh Monzo API access instead of needing to re-confirm it in the Monzo app |
| `MONZO_CLIENT_SECRET` | Server | OAuth client secret for the same client — keep this out of anywhere it could be committed |
| `TELEGRAM_WEBHOOK_SECRET` | Server | A random string Telegram sends with every update, proving it really came from Telegram — generate with `openssl rand -hex 32` |
| `MONZO_WEBHOOK_SECRET` | Server | A random string included in the Monzo webhook URL, proving a transaction really came from Monzo — generate with `openssl rand -hex 32` (use a different value from the one above) |

`config/.env` is gitignored — it will never be committed or overwritten by `git pull`.

**Why the two webhook secrets matter:** `/recieve_telegram/` and `/recieve_monzo/` have to be reachable from the internet for Telegram and Monzo to call them, which means anyone else can call them too — and the domain is easy to find (HTTPS certificates are publicly logged). Without the secrets, a stranger could send your server fake transactions, or fake button taps that skip transactions or approve rules. The server still starts without them, but it logs a warning and sends you a Telegram alert on every startup until both are set.

Once filled in, copy it to the server:
```bash
scp config/.env ubuntu@your_server_ip:~/bank-enrichment/config/.env
```

### 5. Set Up DuckDNS

Telegram requires HTTPS. This project uses [DuckDNS](https://www.duckdns.org) (free) for a domain and Caddy for automatic SSL.

1. Go to [duckdns.org](https://www.duckdns.org) and log in
2. Create a subdomain (e.g. `your-name.duckdns.org`) and point it at your server's public IP
3. Update the `Caddyfile` in the project root — replace `bank-enrichment.duckdns.org` with your subdomain:

```
your-name.duckdns.org {
    reverse_proxy bank-enrichment:8000
}
```

### 6. Open the Firewall

**OS level:**
```bash
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
sudo systemctl enable netfilter-persistent
```

**Cloud provider level** — add ingress rules for TCP ports 80 and 443 from `0.0.0.0/0` in your cloud provider's network security settings. On Oracle Cloud this is done via Network Security Groups on your instance's VNIC.

### 7. Start the Server

```bash
sudo docker compose up -d
```

Docker builds the app image and pulls Caddy, then starts both containers. Caddy automatically obtains a free SSL certificate via Let's Encrypt — requires port 80 open and your DuckDNS domain pointing at the server.

The containers restart automatically on failure and on server reboot.

On every start the server brings the database up to date by applying `sql/postgres/schema.sql`
(it only ever adds tables, columns and views), so there's nothing to set up by hand beyond the
login. If it can't reach the database, check the data box allows this server in its `pg_hba.conf`
and its security list (port 5432 from the server's private address).

A `data/` directory is created automatically containing:
- `server.log` — rotating log file (capped at ~3MB total)
- `monzo_oauth.json` — Monzo access tokens, once step 10 is done

To view live logs:
```bash
sudo docker compose logs -f
```

### 8. Register the Telegram Webhook

Once the server is running, register your URL with Telegram, including the `secret_token` Telegram will then send back with every update. Run this **on the server**, from the repo root — it reads your bot token and secret straight from `config/.env`, so neither ends up in your browser history (replace only the subdomain):

```bash
set -a && . config/.env && set +a
curl -s "https://api.telegram.org/bot${TELEGRAM_API}/setWebhook" \
  -d "url=https://your-name.duckdns.org/recieve_telegram/" \
  -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}"
```

You should get back `{"ok":true,"result":true,...}`. Verify with:

```bash
curl -s "https://api.telegram.org/bot${TELEGRAM_API}/getWebhookInfo"
```

If `last_error_message` there ever says `403 Forbidden`, the secret Telegram has doesn't match `TELEGRAM_WEBHOOK_SECRET` on the server — re-run the `setWebhook` command above.

Only you can use the bot: updates from any chat other than `TELEGRAM_CHAT_ID` are ignored, even with the right secret.

### 9. Register the Monzo Webhook

Monzo can't send custom headers, so its secret goes in the webhook URL itself. Print the exact URL to register — on the server, from the repo root (replace only the subdomain):

```bash
set -a && . config/.env && set +a
echo "https://your-name.duckdns.org/recieve_monzo/?token=${MONZO_WEBHOOK_SECRET}"
```

1. Go to the [Monzo API Playground](https://developers.monzo.com/api/playground)
2. Note your **Account ID** at the top of the playground
3. Click **Register webhook** and send, with the URL printed above:

```json
{
    "account_id": "your_account_id_here",
    "url": "https://your-name.duckdns.org/recieve_monzo/?token=your_monzo_webhook_secret"
}
```

You should get back a webhook ID confirming registration. The token is redacted from the server's request logs, so it won't sit there in plain text.

**Only ever have one webhook registered.** Monzo delivers every transaction to every webhook on the account, so a leftover registration (e.g. an old URL without the token) keeps receiving deliveries the server rejects — and Monzo keeps retrying them. Check with **List webhooks** in the playground and **Delete webhook** any extras.

### 10. Set Up the Monzo OAuth Confidential Client (keeps merchant data flowing)

Monzo's webhook access needs periodically re-confirming in the Monzo app, and when it lapses, the `merchant` field on new transactions silently goes `null` — the category still gets classified fine from the raw description text, but the Telegram card's per-merchant quick-pick buttons fall back to a generic list instead of your actual history for that merchant. A **confidential** OAuth client lets the server silently renew access on a schedule instead of you having to notice and manually reconfirm it in the Monzo app.

1. Go to [developers.monzo.com](https://developers.monzo.com) → **Clients** → **New OAuth Client**.
2. Set **Redirect URLs** to `https://your-name.duckdns.org/oauth/callback`.
3. Set **Confidentiality** to **Confidential** — this is what gets you issued a `refresh_token` at all; a "Public"/non-confidential client never gets one and would need manual re-approval every time it expires.
4. Add the `client_id`/`client_secret` it gives you to the server's `config/.env`:

```
MONZO_CLIENT_ID=your_client_id
MONZO_CLIENT_SECRET=your_client_secret
```

5. Recreate the container so it picks up the new values (editing `.env` alone has no effect on an already-running container):

```bash
docker compose up -d
```

6. Open `https://your-name.duckdns.org/oauth/start` in your browser and log in with your dashboard credentials. It sends you to Monzo's login with a single-use link that expires after 10 minutes — the server only accepts the authorization that comes back from a link it issued itself, so nobody else can complete one on your behalf.

Log in and approve with your PIN/Face ID. You'll land on a page saying "Monzo authorization complete" — the server exchanges the code for an access/refresh token pair itself and stores it in `data/monzo_oauth.json` (gitignored). From there, a background job refreshes it automatically every 4 hours, comfortably inside Monzo's token lifetime, and alerts you on Telegram if a refresh ever fails.

Whether a silent refresh alone is enough to keep `merchant` populated (as opposed to also requiring you to physically reopen the Monzo app sometimes) isn't documented by Monzo — treat this as something to keep an eye on rather than a guaranteed permanent fix.

### 11. Access the Dashboard

Open `https://your-name.duckdns.org/dashboard` in your browser. Log in with the `DASHBOARD_USER` and `DASHBOARD_PASSWORD` you set in `config/.env`.

- **Lifetime Stats** — counted from the transactions (total received, total amount, requests sent, enriched, classified)
- **Current Queue** — paginated list of transactions with where each is up to, and inline controls to enrich, skip, unskip or delete. Each row links to a detail page with the full payload and all actions.
- **Database view** at `/dashboard/db` — the latest raw transactions and context/label events

A follow-up notification system automatically re-sends Telegram reminders at 1 hour, 1 day, 2 days, and 1 week after the initial notification. If there is still no response after a week, the transaction is auto-skipped. A card that couldn't be sent (Telegram down, say) is sent again 10 minutes later; every send to Telegram is itself tried up to three times first.

**Optional — an uptime monitor.** `https://your-name.duckdns.org/health` answers `{"ok": true}` while the server is up and can reach the database, and 503 when it can't. It needs no login and shows nothing else, so a free monitor (UptimeRobot, Better Stack and the like) can check it every few minutes and email you if the server goes down.

---

## Part 2 — Local Setup (the dashboard)

The daily run happens on the server: Docker Compose starts a `worker` container next to the server, which
runs the pipeline every morning at 08:00 London time (see step 5). Your own machine is for the dashboard, and
for running the pipeline by hand when you want to. Both reach the same database over the data box's
WireGuard VPN. Import the config data-vault's
`wg_add_device.sh` made for this machine into the WireGuard app and switch it on; it only routes
the data box's addresses, so the rest of your traffic is unaffected. (On Windows with WSL, WSL
reaches the database through the Windows tunnel — nothing to set up inside WSL.)

> **Windows note — Smart App Control:** Windows 11's Smart App Control can block pandas' compiled binaries from
> loading, even for a normal `pip`/Poetry install, with an error like `ImportError: DLL load failed ... An
> Application Control policy has blocked this file`. It's enforced by a Windows Update, not anything this project
> does, and once it's switched itself to "On" it can't be turned off without reinstalling Windows. It doesn't affect
> Mac/Linux, and if you never hit that error you can skip this and follow the steps below as written.
>
> If you do hit it, run everything in this part through **WSL2** instead of Windows Python — Smart App Control only
> inspects Windows binaries, so it never sees the Linux ones:
> 1. `wsl --install -d Ubuntu-24.04` (one-time; needs a reboot the first time WSL itself is installed)
> 2. Inside the WSL shell: install Poetry and run `poetry install` there, against this same repo at its `/mnt/c/...`
>    path — you don't need a second clone.
> 3. Use `run_dashboard_wsl.sh` (dashboard) and `run_process_wsl.sh` (pipeline only) instead of calling `poetry run`
>    directly — both just `cd` into the project and call Poetry's WSL install. Step 6 below shows how
>    `launch_dashboard.bat` calls the first.
> 4. A Windows-side Poetry install (`poetry env info --executable`) can still be pointed at from an editor for
>    autocomplete — it just can't run pandas-dependent code, only WSL's install can.

### 1. Clone the Repository Locally

If you haven't already cloned the repository on your local machine:

```bash
git clone git@github.com:your-username/bank-enrichment.git
cd bank-enrichment
```

### 2. Install Dependencies

```bash
poetry install
```

### 3. Check config/.env

If you followed Part 1 and filled in `config/.env` locally before copying it to the server, it is already in place. Confirm it contains the `DB_*` values, `TELEGRAM_API`, `TELEGRAM_CHAT_ID` and `CLAUDE_SECRET` — these are the values the local scripts need.

If you created `config/.env` directly on the server and don't have a local copy, create one now:

```bash
cp config/.env.example config/.env
```

Then fill in at minimum the `DB_*` values, `TELEGRAM_API`, `TELEGRAM_CHAT_ID` and `CLAUDE_SECRET` (see the table in step 4 of Part 1).

### 4. Run the Pipeline by Hand (optional)

The worker runs it every morning (step 5); this is for when you don't want to wait:
```bash
poetry run python src/pipeline/process.py
```

This single command does everything in sequence:
1. Connect to the database (it stops here, saying so, if the VPN is down) and seed the default taxonomy if there isn't one — 13 parent categories and ~70 subcategories
2. File any quick-tap answers without calling the LLM (after the second look)
3. Act on any new-category decisions made on Telegram since the last run, then run the LLM classifier (Passes 0-3) on anything still unclassified — a genuinely new category is proposed via Telegram rather than created outright (see [New Category Approval](README.md#new-category-approval) in the README)
4. Act on any monthly-taxonomy-review decisions, then run this month's review if it hasn't already (see [Monthly Taxonomy Review](README.md#monthly-taxonomy-review))
5. Look for new rule suggestions
6. Send any missing weekly or monthly summaries to Telegram

Proposals and suggestions are written to the database; the server sends their Telegram cards
within five minutes.

The default taxonomy covers common personal spending categories and is a starting point — you can rename, restructure, or replace it entirely from the Settings tab in the dashboard. See [Customising Your Taxonomy](#customising-your-taxonomy) below.

Only one run can happen at a time: one started while the worker is running (or the other way round) says so and
stops, rather than classifying the same transactions twice.

Log files are written to `logs/` in the repo (or `LOG_DIR`; on the server, `data/logs/`):
- `bank_enrichment.log` — the run's own log
- `llm_classifier.log` — LLM classification log

`llm_labelling.py` can also be run standalone at any time if you only want to classify.

### 5. The Daily Run on the Server

Nothing to set up: `docker compose up -d` (and every deploy) starts the `worker` container alongside the
server. It runs the pipeline at **08:00 London time** every day, with the same `config/.env` as the server
(it needs `CLAUDE_SECRET` there too), and is capped at 350 MB of memory so a run can never starve the
webhooks.

- **See what it did:** `data/logs/bank_enrichment.log` on the server, or `docker compose logs worker`.
- **Run it now** instead of waiting: `docker compose exec worker python src/pipeline/worker.py --now`.
- **If it can't reach the database**, it tries again every 30 minutes, three times, before alerting.
- **If a run can't start or crashes**, you get a Telegram alert, and it tries again the next morning.
- **On Sundays** it sends a short "all fine", with what's waiting on you. No Sunday message means
  something's wrong.

### 6. View the Dashboard

**Mac/Linux, or Windows unaffected by Smart App Control:**
```bash
poetry run python -m streamlit run src/dashboard/dashboard.py
```
(`python -m streamlit` rather than `streamlit` directly — some Windows security policies block the `streamlit.exe`
binary but allow running it as a Python module. This alone won't help if Smart App Control is blocking pandas
itself — see below.)

**Windows, routing through WSL:** double-click `launch_dashboard.bat` (or the desktop shortcut below) — it opens a
console window running `run_dashboard_wsl.sh` in WSL, then opens your browser once the dashboard is actually ready.
Closing the console window stops it.

It opens in your browser automatically. Tabs:

**Optional — Desktop shortcut (Windows):**

A `launch_dashboard.bat` file is included in the project root — this is how you'll normally start the dashboard day
to day. To add a shortcut to your desktop, run this once in PowerShell:

```powershell
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut("$Home\Desktop\Bank Enrichment.lnk")
$s.TargetPath = "$PWD\launch_dashboard.bat"
$s.WorkingDirectory = "$PWD"
$s.WindowStyle = 1
$s.Save()
```

Run it from the project root so `$PWD` resolves to the right path. You can then right-click the shortcut → Properties → Change Icon to set a custom icon.

| Tab | Description |
|-----|-------------|
| Overview | Standard vs Actual metric cards (income, spend, net, savings rate, invested, saved, transferred/excluded), each with a delta vs the previous period |
| Over Time | Monthly trend charts by category and subcategory, a savings & investing chart, and a monthly totals table |
| Transactions | Full filterable/searchable table — edit Category, Subcategory, Date, and Context inline |
| Category Drill-Down | Pick a parent category to see subcategory breakdowns and transactions |
| Top Merchants | Ranks merchants by spend or frequency, with search and a Top-N filter |
| Subscriptions | Auto-detected recurring payments + manual add, active/inactive toggle, cost totals |
| Settings | Tree view of all categories and their Roles. Add, rename, move, and delete categories. Click 🔗 on any subcategory to view its transactions and bulk-reassign them. Click 🧹 on a parent to wipe its labels so they re-classify on the next run. |

See [Local Dashboard](README.md#local-dashboard) in the README for full details on each tab.

Use the sidebar to filter by date range (quick presets or custom), category, subcategory, free text, or toggle skipped/unclassified rows. The **Refresh data** button reloads from the database without restarting.

### Customising Your Taxonomy

The default taxonomy is seeded automatically on first run, but you own it entirely from there. From the **Settings tab**:

- **Rename** a parent or subcategory — all transaction labels update immediately, and a custom Role you've set is kept
- **Move** a subcategory to a different parent
- **Add** new subcategories inside any parent, or add entirely new parent categories
- **Delete** subcategories or whole parent categories (clears labels from affected transactions; the old labels stay in each transaction's history)
- **Wipe labels** for a parent category — transactions keep their context but lose their LLM label, so they'll be re-classified on the next `process.py` run against your updated structure
- **View transactions** per subcategory — see exactly what's in each bucket and bulk-reassign if needed

If you want to start fresh with a completely different taxonomy, use the Danger Zone at the
bottom of the Settings tab to wipe the entire taxonomy structure, then build your own from
scratch. The default taxonomy is only ever seeded when there are no categories at all, so it
won't get added back in alongside whatever you build. Your context sentences are never
touched — only the labels change.

### 7. Utility Scripts

Print all transactions and stats to the terminal:
```bash
poetry run python src/pipeline/database_functions.py
```

Wiping labels or the entire taxonomy is done from the **Settings tab** in the dashboard 
rather than a standalone script — see [Customising Your Taxonomy](#customising-your-taxonomy) 
above. Those are destructive, hard-to-undo operations, so they live behind the dashboard's 
confirmation guardrails rather than a bare CLI script.

### 8. Running Tests Locally

The test suite mocks every network service, so it runs without a live server or real credentials.
The database tests need an empty Postgres to build throwaway schemas in — point `TEST_POSTGRES_URL`
at one (never the real database), or they're skipped:

```bash
poetry install --with dev
TEST_POSTGRES_URL=postgresql://postgres@localhost:5432/postgres poetry run pytest
```

This is the same command the `test` job in CI runs on every pull request, against a Postgres
service it starts empty for the run.

---

## Continuous Deployment

Merging a pull request into `main` automatically tests and deploys the server — no manual SSH step needed day-to-day. A GitHub Actions workflow (`.github/workflows/ci.yml`) runs two jobs:

1. **`test`** — runs on every pull request and every push to `main`: installs dependencies and runs `poetry run pytest`.
2. **`deploy`** — runs only on a push to `main`, and only if `test` passed: SSHs into the server and runs `git pull && docker compose up -d --build && docker image prune -f`.

### One-Time CI/CD Setup

**1. Generate a dedicated key for GitHub Actions to log into the server** — on the server:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/gh_deploy -C "github-actions-deploy" -N ""
cat ~/.ssh/gh_deploy.pub >> ~/.ssh/authorized_keys
cat ~/.ssh/gh_deploy   # copy the private key output
```

**2. Add three repository secrets** at `https://github.com/your-username/bank-enrichment/settings/secrets/actions` → **New repository secret**:

| Secret | Value |
|---|---|
| `DEPLOY_HOST` | Your server's hostname or IP |
| `DEPLOY_USER` | The SSH user (e.g. `ubuntu`) |
| `DEPLOY_SSH_KEY` | The private key from step 1 |

**3. Make sure the server can `git pull` non-interactively.** The deploy script runs unattended, so whatever SSH key the server uses to authenticate to GitHub must have no passphrase. If your existing checkout uses your own passphrase-protected personal key, generate a separate read-only deploy key instead:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/repo_deploy_key -C "bank-enrichment-deploy" -N ""
cat ~/.ssh/repo_deploy_key.pub
```
Add that public key at `https://github.com/your-username/bank-enrichment/settings/keys` → **Add deploy key** (leave "Allow write access" unchecked — pulling only needs read), then point git at it for GitHub specifically:
```bash
cat >> ~/.ssh/config << 'EOF'
Host github.com
    IdentityFile ~/.ssh/repo_deploy_key
    IdentitiesOnly yes
EOF
```
Test it: `cd ~/bank-enrichment && git pull` should complete with no prompt.

**4. Make sure the deploy user can run Docker without `sudo`** — a non-interactive SSH session can't answer a sudo password prompt:
```bash
groups ubuntu   # check the list includes "docker"
sudo usermod -aG docker ubuntu   # if missing — then fully log out and back in
```

Once all four steps are done, every merge to `main` deploys automatically. Watch it run at `https://github.com/your-username/bank-enrichment/actions`.

### Manual Deploy (fallback)

If you need to deploy without going through a PR:

```bash
cd ~/bank-enrichment
git pull
sudo docker compose up -d --build && sudo docker image prune -f
```

**If the database schema has changed**, the server applies `sql/postgres/schema.sql` on restart,
which adds new tables, columns and views — nothing to do by hand. It never drops or rewrites
data; a change that needs to is a one-off script, run deliberately, after a backup.

**If the `Caddyfile` changed**, note that `docker compose up -d --build` won't pick it up — Caddy only reads its config at container start, and docker compose doesn't detect content-only changes to bind-mounted files. Reload it manually:
```bash
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```
