## Monzo Webhook Setup

### Prerequisites
- A Monzo account with developer access enabled at developers.monzo.com
- A server with a public IP address and Docker installed

### Telegram Bot Setup

The system sends push notifications via a Telegram bot when a transaction arrives.

#### 1. Create a bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts to choose a name and username
3. BotFather will give you a token that looks like `123456789:ABCdef...` — this is your `TELEGRAM_API` key

#### 2. Get your chat ID

1. Search for your bot's username in Telegram and send it any message
2. Visit the following URL in your browser (replace with your token):

```
https://api.telegram.org/bot{YOUR_TOKEN}/getUpdates
```

3. In the JSON response, find `result[0].message.chat.id` — that number is your `TELEGRAM_CHAT_ID`

---

### Registering the Webhook

1. Go to the [Monzo API Playground](https://developers.monzo.com/api/playground)
2. Note your **Account ID** displayed at the top of the playground
3. Click **Register webhook** in the left sidebar
4. In the request body, replace the placeholder values with your own:

```json
{
    "account_id": "your_account_id_here",
    "url": "http://your_server_ip:8000/recieve_monzo/"
}
```

5. Click **Send** — you should get back a webhook ID confirming registration

### Setting Up the Server

The webhook listener runs as a Docker container on a Linux server.
The server receives transaction events from Monzo in real time and stores them
for processing by your local machine.

---

#### 1. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
```

#### 2. Clone the Repository

SSH into your server and clone the project:

```bash
git clone git@github.com:your-username/bank-enrichment.git
cd bank-enrichment
```

#### 3. Configure Environment Variables

Copy the example environment file and fill in your own values:

```bash
cp config/.env.example config/.env
nano config/.env
```

The file contains the following keys:

| Key | Description |
|---|---|
| `DASHBOARD_USER` | Username for the dashboard login |
| `DASHBOARD_PASSWORD` | Password for the dashboard login |
| `TELEGRAM_API` | Your Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your personal Telegram chat ID (see Telegram Bot Setup above) |
| `OPENAI_SECRET` | Your OpenAI API key |
| `CLAUDE_SECRET` | Your Anthropic API key |

`config/.env` is gitignored and stays on your server only — `git pull` will never overwrite it.

To get it onto the server you have two options:

**Option A — Copy from your local machine:**
```bash
scp config/.env ubuntu@your_server_ip:~/bank-enrichment/config/.env
```

**Option B — Create it directly on the server via SSH:**
```bash
nano ~/bank-enrichment/config/.env
```
Paste your values in, then `Ctrl+X`, `Y`, `Enter` to save.

#### 4. Open the Firewall

The server listens on port 8000. You need to open this at two levels:

**OS level:**
```bash
sudo iptables -I INPUT -p tcp --dport 8000 -j ACCEPT
```

**Cloud provider level:**

Add an ingress security rule for TCP port 8000 with source `0.0.0.0/0` in your
cloud provider's network security settings. On Oracle Cloud this is done via
Network Security Groups on your instance's VNIC.

#### 5. Start the Server

```bash
sudo docker compose up -d
```

Docker will build the image, install all dependencies, and start the server.
The container restarts automatically on failure and on server reboot.

A `data/` directory is created automatically in the project folder containing:
- `bank_enrichment_server.db` — the DuckDB database
- `server.log` — rotating log file (capped at ~3MB total)

To view live logs:

```bash
sudo docker compose logs -f
```

#### 6. Access the Dashboard

Open `http://your_server_ip:8000/dashboard` in your browser. You will be prompted
for the `DASHBOARD_USER` and `DASHBOARD_PASSWORD` you set in `config/.env`.

The dashboard has two sections:

- **Lifetime Stats** — persistent counters (total received, total amount, notifications sent, enriched, processed) stored in a dedicated `stats` table that survives queue clears
- **Current Queue** — live status breakdown and full list of all pending and enriched transactions, each linking to a detail page

#### 7. Deploying Updates

When you push changes to the repository, SSH into your server and run:

```bash
cd ~/bank-enrichment
git pull
sudo docker compose up -d --build && sudo docker image prune -f
```

**If the database schema has changed**, check the release notes. Most additive changes (new columns, new tables) apply automatically on restart via migrations in `init_db()` — no wipe needed. If a breaking change requires a full reset:

```bash
sudo docker compose down
git pull
rm -f data/bank_enrichment_server.db
sudo docker compose up -d --build && sudo docker image prune -f
```
