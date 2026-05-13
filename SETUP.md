## Monzo Webhook Setup

### Prerequisites
- A Monzo account with developer access enabled at developers.monzo.com
- A server with a public IP address running this application

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

The webhook listener runs as a persistent background service on a Linux server.
The server receives transaction events from Monzo in real time and queues them
for processing by your local machine.

These instructions assume Ubuntu 22.04. Any Linux distribution with systemd
will work with minor adjustments.

---

#### 1. Clone the Repository

SSH into your server and clone the project:

```bash
git clone git@github.com:your-username/bank-enrichment.git
cd bank-enrichment
```

#### 2. Install Dependencies

This project uses [Poetry](https://python-poetry.org/) for dependency management.

```bash
pip install poetry
export PATH="$HOME/.local/bin:$PATH"
poetry install
```

> **Note:** Add the export line to your `~/.bashrc` to make it permanent.

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

#### 4. Run as a System Service

To ensure the server starts automatically on boot and restarts on failure,
register it as a systemd service.

Create the service file:

```bash
sudo nano /etc/systemd/system/bank-enrichment.service
```

Paste the following, updating `ExecStart` with the path to your Poetry
virtualenv. You can find this by running `poetry env info --path`:

```ini
[Unit]
Description=Bank Enrichment Webhook Listener
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/bank-enrichment
ExecStart=/path/to/virtualenv/bin/python src/server_scripts/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable bank-enrichment
sudo systemctl start bank-enrichment
```

Verify it is running:

```bash
sudo systemctl status bank-enrichment
```

You should see `active (running)` in the output.

#### 5. Deploying Updates

When you push changes to the repository, SSH into your server and run:

```bash
cd ~/bank-enrichment
git pull
sudo systemctl restart bank-enrichment
```

---

> **Tip:** To find your Poetry virtualenv path run `poetry env info --path`
> inside the project directory and use the output in your `ExecStart` line.


