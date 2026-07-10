## Prerequisites

- A Monzo account with developer access enabled at [developers.monzo.com](https://developers.monzo.com)
- A server with a public IP address and Docker installed (e.g. Oracle Cloud free tier)
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
| `TELEGRAM_API` | Server | Your Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Server | Your personal Telegram chat ID (see step 3) |
| `LOCAL_API_KEY` | Both | A random hex key — generate one at [browserling.com/tools/random-hex](https://www.browserling.com/tools/random-hex) (set length to 64) |
| `SERVER_URL` | Local | Your server's full URL with no trailing slash — e.g. `https://your-name.duckdns.org` |
| `DB_PATH` | Local | Full path for your local DuckDB database — e.g. `C:/Users/you/Documents/bank_enrichment.db` |
| `CLAUDE_SECRET` | Local | Your Anthropic API key from [console.anthropic.com](https://console.anthropic.com) — used by the LLM classifier |

`config/.env` is gitignored — it will never be committed or overwritten by `git pull`.

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

A `data/` directory is created automatically containing:
- `bank_enrichment_server.db` — the server DuckDB database
- `server.log` — rotating log file (capped at ~3MB total)

To view live logs:
```bash
sudo docker compose logs -f
```

### 8. Register the Telegram Webhook

Once the server is running, register your URL with Telegram (replace with your token and subdomain):

```
https://api.telegram.org/bot{YOUR_TOKEN}/setWebhook?url=https://your-name.duckdns.org/recieve_telegram/
```

You should get back `{"ok":true,"result":true}`. Verify with:

```
https://api.telegram.org/bot{YOUR_TOKEN}/getWebhookInfo
```

### 9. Register the Monzo Webhook

1. Go to the [Monzo API Playground](https://developers.monzo.com/api/playground)
2. Note your **Account ID** at the top of the playground
3. Click **Register webhook** and send:

```json
{
    "account_id": "your_account_id_here",
    "url": "https://your-name.duckdns.org/recieve_monzo/"
}
```

You should get back a webhook ID confirming registration.

### 10. Access the Dashboard

Open `https://your-name.duckdns.org/dashboard` in your browser. Log in with the `DASHBOARD_USER` and `DASHBOARD_PASSWORD` you set in `config/.env`.

- **Lifetime Stats** — persistent counters (total received, total amount, notifications sent, enriched, processed)
- **Current Queue** — paginated list of all unprocessed transactions with inline controls to enrich, skip, or delete. Each row links to a detail page with the full payload and all actions.
- **Database view** at `/dashboard/db` — inspect the raw `stats` and `webhook_queue` tables directly

A follow-up notification system automatically re-sends Telegram reminders at 1 hour, 1 day, 2 days, and 1 week after the initial notification. If there is still no response after a week, the transaction is auto-skipped.

---

## Part 2 — Local Script Setup

The local processing script runs on your own machine and pulls enriched transactions from the server into a local DuckDB database.

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

If you followed Part 1 and filled in `config/.env` locally before copying it to the server, it is already in place. Confirm it contains `SERVER_URL`, `DB_PATH`, `LOCAL_API_KEY`, and `CLAUDE_SECRET` — these are the values the local scripts need.

If you created `config/.env` directly on the server and don't have a local copy, create one now:

```bash
cp config/.env.example config/.env
```

Then fill in at minimum `LOCAL_API_KEY`, `SERVER_URL`, and `DB_PATH` (see the table in step 4 of Part 1).

### 4. Run the Scripts

**Run the pipeline:**
```bash
poetry run python src/local_scripts/process.py
```

This single command does everything in sequence:
1. Create the local database file at `DB_PATH` (first run only)
2. Create all tables (transactions, parent_categories, subcategories)
3. Seed the default taxonomy — 13 parent categories and ~70 subcategories are inserted on first run (skipped if they already exist)
4. Fetch all enriched transactions from the server
5. Write them to the local database
6. Mark them as processed on the server
7. Run the LLM classifier — assigns parent categories and subcategories to any unclassified transactions using Claude
8. Send any missing monthly summaries to Telegram

The default taxonomy covers common personal spending categories and is a starting point — you can rename, restructure, or replace it entirely from the Settings tab in the dashboard. See [Customising Your Taxonomy](#customising-your-taxonomy) below.

Two log files are created automatically alongside the database:
- `bank_enrichment.log` — pull and processing log
- `llm_classifier.log` — LLM classification log

`llm_labelling.py` can also be run standalone at any time if you want to re-classify without pulling new transactions.

### 5. Schedule the Task

First, get the path to the Poetry Python executable:
```powershell
poetry env info --executable
```

**Windows — Task Scheduler:**

1. Open **Task Scheduler** → **Create Basic Task**
2. Name it `Bank Enrichment` → Next
3. Trigger: **Daily** → set your preferred time (e.g. 08:00) → Next
4. Action: **Start a program** → Next
5. **Program/script**: paste the full path from `poetry env info --executable`
6. **Arguments**: `src\local_scripts\process.py`
7. **Start in**: your project root (e.g. `C:\Users\you\Projects\bank-enrichment`)
8. Finish → open Properties → **General** tab → tick **Run whether user is logged on or not**

The script is safe to re-run manually at any time.

**Mac/Linux — cron:**
```bash
crontab -e
```
Add one line (adjust path and time as needed):
```
0 8 * * * cd /path/to/bank-enrichment && poetry run python src/local_scripts/process.py
```

### 6. View the Dashboard

Launch:
```bash
poetry run python -m streamlit run src/local_scripts/dashboard.py
```

> **Note (Windows):** Use `python -m streamlit` rather than `streamlit` directly — some Windows security policies block the `streamlit.exe` binary but allow running it as a Python module.

It opens in your browser automatically. Tabs:

**Optional — Desktop shortcut (Windows):**

A `launch_dashboard.bat` file is included in the project root. To add a shortcut to your desktop, run this once in PowerShell:

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
| Overview | Standard vs Actual metric cards (income, spend, net, savings rate, invested, transferred/excluded), each with a delta vs the previous period |
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
- **Delete** subcategories or whole parent categories (clears labels from affected transactions)
- **Wipe labels** for a parent category — transactions keep their context but lose their LLM label, so they'll be re-classified on the next `process.py` run against your updated structure
- **View transactions** per subcategory — see exactly what's in each bucket and bulk-reassign if needed

If you want to start fresh with a completely different taxonomy, use the Danger Zone at the
bottom of the Settings tab to wipe the entire taxonomy structure, then build your own from
scratch. The default taxonomy is only ever seeded into a genuinely empty database, so it
won't get added back in alongside whatever you build. Your context sentences are never
touched — only the labels change.

### 7. Utility Scripts

Print all transactions and stats to the terminal:
```bash
poetry run python src/local_scripts/database_functions.py
```

Wiping labels or the entire taxonomy is done from the **Settings tab** in the dashboard 
rather than a standalone script — see [Customising Your Taxonomy](#customising-your-taxonomy) 
above. Those are destructive, hard-to-undo operations, so they live behind the dashboard's 
confirmation guardrails rather than a bare CLI script.

### 8. Running Tests Locally

The test suite mocks all network/DB dependencies, so it runs without a live server or real credentials:

```bash
poetry install --with dev
poetry run pytest
```

This is the same command the `test` job in CI runs on every pull request.

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

**If the database schema has changed**, most additive changes (new columns, new tables) apply automatically on restart via migrations in `init_db()` — no wipe needed. If a breaking change requires a full reset:

```bash
sudo docker compose down
git pull
rm -f data/bank_enrichment_server.db
sudo docker compose up -d --build && sudo docker image prune -f
```

**If the `Caddyfile` changed**, note that `docker compose up -d --build` won't pick it up — Caddy only reads its config at container start, and docker compose doesn't detect content-only changes to bind-mounted files. Reload it manually:
```bash
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```
