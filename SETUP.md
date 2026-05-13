## Monzo Webhook Setup

### Prerequisites
- A Monzo account with developer access enabled at developers.monzo.com
- A server with a public IP address and Docker installed

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

#### 3. Open the Firewall

The server listens on port 8000. You need to open this at two levels:

**OS level:**
```bash
sudo iptables -I INPUT -p tcp --dport 8000 -j ACCEPT
```

**Cloud provider level:**

Add an ingress security rule for TCP port 8000 with source `0.0.0.0/0` in your
cloud provider's network security settings. On Oracle Cloud this is done via
Network Security Groups on your instance's VNIC.

#### 4. Start the Server

```bash
docker compose up -d
```

Docker will build the image, install all dependencies, and start the server.
The container restarts automatically on failure and on server reboot.

A `data/` directory is created automatically in the project folder containing:
- `bank_enrichment_server.db` — the DuckDB database
- `server.log` — rotating log file (capped at ~3MB total)

To view live logs:

```bash
docker compose logs -f
```

#### 5. Deploying Updates

When you push changes to the repository, SSH into your server and run:

```bash
cd ~/bank-enrichment
git pull
docker compose up -d --build
```

**If the database schema has changed**, wipe the existing database first so it is recreated cleanly:

```bash
docker compose down
git pull
rm -f data/bank_enrichment_server.db
docker compose up -d --build
```
