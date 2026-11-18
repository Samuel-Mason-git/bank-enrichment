# Setup

Five steps. The setup command does most of the work: it asks for each credential, checks each one as you
go, registers the Telegram webhook and writes `config/.env`. Everything after the five steps is reference
material, for doing a step by hand or understanding what runs where. Material you only need for a
particular setup (a small VM, a database on another machine, Windows Smart App Control, automatic deploys)
is on [Advanced setup](docs/setup/advanced.md).

## The five steps

### 1. Get these ready

- **A Telegram bot.** In Telegram, message **@BotFather**, send `/newbot` and follow the prompts. It
  gives you a token like `123456789:ABCdef...`.
- **An Anthropic API key**, from [console.anthropic.com](https://console.anthropic.com).
- **A PostgreSQL database that you already run**, reachable from the server and from your own machine.
  This app doesn't install one. Create an empty database and a login that owns it. The server creates
  the tables on its first start. Back it up however you already back up that database.
- **A server with a public IP address**, with Git and Docker on it (`curl -fsSL https://get.docker.com | sh`).
  Oracle Cloud's free tier works: see [Advanced setup](docs/setup/advanced.md#a-small-server) for the
  swapfile a 1 GB machine needs.
- **A free [DuckDNS](https://www.duckdns.org) subdomain** (e.g. `your-name.duckdns.org`) pointing at the
  server's IP. Telegram and Monzo only deliver to HTTPS addresses, and Caddy gets the certificate for this name.
- **A Monzo developer login**, at [developers.monzo.com](https://developers.monzo.com).
- **On your own machine:** Git, Python 3.11 or newer and [Poetry](https://python-poetry.org/docs/#installation).
  On Windows, see [Windows](#windows) first.

### 2. Run the setup command on your own machine

```bash
git clone https://github.com/your-username/bank-enrichment.git
cd bank-enrichment
poetry install
poetry run python src/pipeline/setup_wizard.py
```

It asks for the bot token, finds your chat with the bot (you send the bot a message when asked), then the
Anthropic key, the database's details, a folder for backups, whether to keep a [bronze copy](#optional-a-bronze-copy)
(none by default), a login name and password for linking Monzo, and the server's public address. Give the
address as `https://your-name.duckdns.org` so it can register the Telegram webhook.

Each answer is checked as you go: the bot token with Telegram, the key with Anthropic, the database by
connecting to it. `config/.env` is only written once every check passes, and the old file, if there was
one, is kept beside it. Both are readable only by you (on macOS and Linux by file permissions, on Windows
by an access list set with `icacls`). The command generates the two webhook secrets itself and finishes by
taking a first test backup.

`config/.env` is gitignored. Keep `config/` out of anything you share.

### 3. Start the server

On the server:

```bash
git clone https://github.com/your-username/bank-enrichment.git
cd bank-enrichment
```

1. In `Caddyfile`, replace `bank-enrichment.duckdns.org` with your subdomain.
2. Open ports 80 and 443, in the server's firewall and in your cloud provider's network rules. See
   [Opening the firewall](docs/setup/advanced.md#opening-the-firewall) for the commands.
3. Copy `config/.env` over from your machine:
   ```bash
   scp config/.env ubuntu@your_server_ip:~/bank-enrichment/config/.env
   ```
4. Start it:
   ```bash
   sudo docker compose up -d
   ```

This builds the app and starts three containers: the server that takes the webhooks, the worker that
runs the pipeline every hour from 08:00 to 22:00 London time, and Caddy, which gets the HTTPS certificate.
They restart on failure and when the server reboots. On every start the server brings the database's
tables up to date. If it can't reach the database, check the database allows connections from the
server's address (`pg_hba.conf`) and that its firewall lets port 5432 through.

### 4. Connect Monzo

**The webhook, which sends each payment as it happens.** On the server, print the address to register:

```bash
set -a && . config/.env && set +a
echo "https://your-name.duckdns.org/receive_monzo/?token=${MONZO_WEBHOOK_SECRET}"
```

In the [Monzo API Playground](https://developers.monzo.com/api/playground), note your **Account ID**, then
use **Register webhook** with that account ID and the address printed above. Keep only one webhook
registered: Monzo sends every payment to each one, so check **List webhooks** and delete any old ones.

**The login, which keeps merchant names coming.** Without it, Monzo's access lapses every so often and
new payments arrive without the merchant's name.

1. At [developers.monzo.com](https://developers.monzo.com), go to **Clients** → **New OAuth Client**. Set
   **Redirect URLs** to `https://your-name.duckdns.org/oauth/callback` and **Confidentiality** to
   **Confidential**. Only a confidential client can renew its own access.
2. Add what it gives you to `config/.env` on the server:
   ```
   MONZO_CLIENT_ID=your_client_id
   MONZO_CLIENT_SECRET=your_client_secret
   ```
3. Run `sudo docker compose up -d` again, so the containers read the new values.
4. Open `https://your-name.duckdns.org/oauth/start`, log in with the name and password you gave the setup
   command, then approve in Monzo. You land on "Monzo authorization complete".

From then on the server renews the access every 4 hours, and tells you on Telegram if a renewal fails.

### 5. Open the dashboard and run the setup check

On your own machine:

```bash
poetry run python -m streamlit run src/dashboard/dashboard.py
```

On Windows, double-click `launch_dashboard.bat` instead (see [Windows](#windows)).

Go to **Settings → Setup check** and press **Run the checks**. It checks the database, that the worker is
running on schedule, that Monzo payments are arriving, that the Telegram webhook is registered and
delivering, and that Anthropic accepts the Claude key. Each problem says what to do about it. See
[Settings](docs/money/settings.md#setup-check).

Then make a payment with your Monzo card. A Telegram card asking what it was should arrive within a few
seconds.

---

## Windows

Windows 11's Smart App Control can stop pandas from loading, with `ImportError: DLL load failed ... An
Application Control policy has blocked this file`. If you get that error, run the dashboard inside WSL:

1. Install WSL with a Linux distro, from an administrator PowerShell: `wsl --install -d Ubuntu`. The
   first install needs a restart.
2. Inside WSL, install Poetry: `sudo apt install -y python3 python3-venv pipx && pipx install poetry`.
   Use this same folder at its `/mnt/c/...` path; there's no need for a second clone.
3. Double-click `launch_dashboard.bat`. It uses your default WSL distro (skipping Docker Desktop's own),
   installs the project's packages the first time, and opens your browser when the dashboard is ready.
   Closing its window stops the dashboard. To use a different distro, set `BANK_WSL_DISTRO`, e.g.
   `setx BANK_WSL_DISTRO Ubuntu-24.04`.

If WSL or Poetry is missing, the launcher's window says so and what to run. The setup command (step 2) can
run inside WSL the same way: `wsl`, `cd` to the folder, then the commands in step 2.
[Advanced setup](docs/setup/advanced.md#windows-smart-app-control) has more on why, and a desktop shortcut.

---

## Reference

### What's in config/.env

One file, used by the server, the worker and the dashboard. The setup command writes all of it except
the lines marked optional and the Monzo client. To write it by hand instead, start from the example:
`cp config/.env.example config/.env`.

| Key | Used by | What it is |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Both | Your bot's token from @BotFather. The pipeline sends its summaries and alerts with it too |
| `TELEGRAM_CHAT_ID` | Both | Your chat with the bot. Updates from any other chat are ignored |
| `ANTHROPIC_API_KEY` | Both | Your Anthropic key, for classifying payments and the Ask page |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Both | The database login for this project. `DB_HOST` is the database's address as the server and your machine reach it |
| `TELEGRAM_WEBHOOK_SECRET` | Server | A random string Telegram sends with every update, proving it came from Telegram |
| `MONZO_WEBHOOK_SECRET` | Server | A random string in the Monzo webhook address, proving a payment came from Monzo. A different value from the one above |
| `MONZO_LOGIN_USER`, `MONZO_LOGIN_PASSWORD` | Server | The login in front of `/oauth/start` |
| `MONZO_CLIENT_ID`, `MONZO_CLIENT_SECRET` | Server | The confidential Monzo client from step 4 |
| `BACKUP_DIR` | Local | Where the dashboard's **Back up now** saves, unless you pick a folder on Settings → Backups |
| `AGENT_DB_USER`, `AGENT_DB_PASSWORD` | Both (optional) | The Ask page's read-only login. See [The Ask page's login](#the-ask-pages-login) |
| `BRONZE_*` | Server (optional) | Where the bronze copy goes. See [Optional: a bronze copy](#optional-a-bronze-copy) |
| `LOG_DIR` | Local (optional) | Where the pipeline's log files go. Default `logs/` in the repo |

**Why the two webhook secrets matter.** `/receive_telegram/` and `/receive_monzo/` have to be reachable
from the internet, so anyone can call them, and the domain is easy to find (HTTPS certificates are
publicly logged). Without the secrets, a stranger could send your server fake payments, or fake button
taps that skip payments or approve rules. The server won't start until both are set: it stops with an
error naming the missing one. Generate one by hand with `openssl rand -hex 32`.

After changing `config/.env` on the server, run `sudo docker compose up -d` so the containers read it.

### Registering the Telegram webhook by hand

The setup command does this if you give it the server's address. To do it yourself, on the server from
the repo root:

```bash
set -a && . config/.env && set +a
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  -d "url=https://your-name.duckdns.org/receive_telegram/" \
  -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}"
```

You should get back `{"ok":true,"result":true,...}`. Check it with
`curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getWebhookInfo"`. If `last_error_message` there
says `403 Forbidden`, the secret Telegram has doesn't match `TELEGRAM_WEBHOOK_SECRET`: run `setWebhook` again.

To find your chat ID by hand, send the bot a message, then open
`https://api.telegram.org/bot<your token>/getUpdates`: it's `result[0].message.chat.id`.

Optional: send BotFather `/setcommands`, pick your bot, and send these lines, so the commands are offered
as you type:

```
ask - Ask about your money
budget - This month's budgets
health - Is the app working
help - What the bot can do
```

### The Ask page's login

The Ask page and `/ask` query the database through a second, **read-only** login, never the main one. In
the database, as its owner:

```sql
CREATE ROLE bank_enrichment_agent LOGIN PASSWORD 'choose-one';
```

Then add `AGENT_DB_USER=bank_enrichment_agent` and `AGENT_DB_PASSWORD` to `config/.env` on both machines,
and run `sudo docker compose up -d` on the server. The server grants that role what `sql/postgres/schema.sql`
gives it on its next start: read access to the `transaction_roles` view, subscriptions and categories.
Without it, Ask is off.

### Optional: a bronze copy

A raw save of everything the app receives, as plain files, kept apart from the database so it survives
if the database doesn't. The app doesn't need it to run. The setup command asks where it goes: none (the
default), a folder, or a bucket. A folder or bucket gets a copy every day, after the worker's first run
of the day. See [Backups](docs/data/backups.md#a-daily-copy-somewhere-else) for the settings and what's kept.

### What runs on the server

There are no web pages on the server: you see and manage everything from the dashboard. The server takes
the webhooks, the Monzo login and `/health`.

- **The worker** runs the pipeline every hour on the hour from 08:00 to 22:00 London time, and not
  overnight, so no card arrives at 3am. A run with nothing new to do makes no Claude calls. The weekly and
  monthly summaries go out in the morning's first run. It's capped at 350 MB of memory so a run can never
  starve the webhooks.
- **If a run can't reach the database or crashes**, you get one Telegram alert, and it tries again the
  next hour. It tells you once runs work again. The same kind of alert (out of Claude credit, say) is
  sent at most once a day.
- **If the worker stops running altogether** (its container stopped, say), the server notices within half
  an hour of a missed run and tells you on Telegram, once, and again when it's back.
- **On Sundays** it sends a short "all fine", with what's waiting on you.
- **Follow-ups:** a card you haven't answered is sent again after 1 hour, 1 day, 2 days and 1 week, then
  skipped.

Useful commands, on the server:

- See what it did: `data/logs/bank_enrichment.log`, or `sudo docker compose logs worker`.
- Run it now: `sudo docker compose exec worker python src/pipeline/worker.py --now`.
- Live logs: `sudo docker compose logs -f`.

`data/` on the server holds `server.log` (rotating, about 3 MB at most) and `monzo_oauth.json`, Monzo's
access tokens.

### Running the pipeline by hand

The worker runs it every hour; this is for not waiting. On your machine:

```bash
poetry run python src/pipeline/process.py
```

On Windows through WSL, `scripts/run_process_wsl.sh` does the same. In order, it:

1. Connects to the database (stopping, and saying so, if it can't) and sets up the default categories if
   there are none: 13 parent categories and about 70 subcategories.
2. Files any quick-tap answers without calling Claude.
3. Acts on new-category decisions made on Telegram, then classifies anything still unclassified. A
   genuinely new category is proposed on Telegram rather than created (see
   [New categories](docs/classification/new-categories.md#new-category-approval)).
4. Acts on the monthly category review's decisions, then runs this month's review if it hasn't run (see
   [Monthly taxonomy review](docs/classification/new-categories.md#monthly-taxonomy-review)).
5. Looks for new rule suggestions.
6. Sends any missing weekly or monthly summaries.

Only one run happens at a time: one started while the worker is running says so and stops. Logs go to
`logs/` in the repo (or `LOG_DIR`): `bank_enrichment.log` for the run, `llm_classifier.log` for the
classifier.

### The dashboard

Use the sidebar to filter by date, category, subcategory or text. **Refresh data** reloads from the
database. See [Local dashboard](docs/money/dashboard.md) for each page.

### Customising Your Taxonomy

The default categories are set up on the first run, and you own them from there. On the **Settings** page
you can rename, move, add and delete categories, set each one's role, see a subcategory's transactions and
reassign them, and wipe a parent's labels so its transactions are classified again on the next run.
Deleting a category clears its labels; the old labels stay in each transaction's history.

To start from scratch, the Danger zone on the Settings page wipes the whole taxonomy. The defaults are
only ever set up when there are no categories at all, so they won't come back alongside yours. Your
context sentences are never touched, only the labels. See [Categories and roles](docs/classification/categories-and-roles.md).

### Running the tests

The tests stand in for every network service, so they run without a server or real credentials. The
database tests need an empty Postgres to make throwaway schemas in. Point `TEST_POSTGRES_URL` at one
(never the real database), or they're skipped:

```bash
poetry install --with dev
TEST_POSTGRES_URL=postgresql://postgres@localhost:5432/postgres poetry run pytest
```

CI runs the same on every pull request, against an empty Postgres it starts for the run.

### Deploying updates

Pull and rebuild on the server:

```bash
cd ~/bank-enrichment
git pull
sudo docker compose up -d --build && sudo docker image prune -f
```

The server applies `sql/postgres/schema.sql` on restart, which only adds tables, columns and views. To
deploy automatically on every merge, see [Automatic deploys](docs/setup/advanced.md#automatic-deploys).
