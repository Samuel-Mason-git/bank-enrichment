# Advanced setup

Material from [SETUP.md](../../SETUP.md) that only some setups need. Each section stands on its own.

## A small server

On a small machine (Oracle's free 1 GB AMD Micro, say), add a swapfile. With no swap, a brief spike in
memory use (a burst of webhooks, a dependency update, Caddy renewing its certificate) gets a container
killed mid-request. With swap it's a moment of slowness instead. From the repo root on the server:

```bash
sudo ./scripts/setup_swap.sh        # 2G by default; pass a size to change it, e.g. sudo ./scripts/setup_swap.sh 1G
```

It creates `/swapfile`, adds it to `/etc/fstab` so it survives reboots, and sets `vm.swappiness=10` so
the system only swaps when memory is really short. It's safe to run again: anything already in place is
left alone. Check with `free -h`: the `Swap:` line shows the size you chose.

The worker is capped at 350 MB (`mem_limit` in `docker-compose.yml`) so a run can't take the memory the
webhooks need.

## Opening the firewall

Caddy needs ports 80 and 443: 80 for getting the certificate, 443 for the webhooks.

**In the server's own firewall:**

```bash
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
sudo systemctl enable netfilter-persistent
```

**In your cloud provider's network rules:** allow TCP 80 and 443 in from `0.0.0.0/0`. On Oracle Cloud,
that's the Network Security Group on the instance's VNIC.

## A database on another machine

The server and your own machine both need to reach the database. If it's on another machine, reach it
over a private network rather than opening Postgres to the internet. WireGuard works well: set it up so
it routes only the database's addresses. On Windows with WSL, WSL reaches the database through the
Windows tunnel, so there's nothing to set up inside WSL.

On the database's side, allow the server's and your machine's addresses in `pg_hba.conf`, and let port
5432 through its firewall from them.

## Windows: Smart App Control

Windows 11's Smart App Control can stop pandas' compiled files from loading, even from a normal Poetry
install, with an error like `ImportError: DLL load failed ... An Application Control policy has blocked
this file`. A Windows update turns it on, not this project, and once it has switched itself to "On" it
can't be turned off without reinstalling Windows. Mac and Linux aren't affected.

Smart App Control only inspects Windows programs, so running the dashboard inside WSL avoids it.
[SETUP.md](../../SETUP.md#windows) has the steps. What the pieces do:

- `launch_dashboard.bat` finds a WSL distro (`BANK_WSL_DISTRO` if you set it, otherwise your default,
  skipping Docker Desktop's own), runs `scripts/run_dashboard_wsl.sh` in it, and opens your browser once
  the dashboard answers. If WSL, a distro or Poetry is missing, its window says what to run.
- `scripts/run_dashboard_wsl.sh` and `scripts/run_process_wsl.sh` find Poetry (on your `PATH`, then
  `~/.local/bin`, or wherever `POETRY` points), install the project's packages if they aren't there yet,
  then start the dashboard or run the pipeline once.
- A Windows-side Poetry install can still be pointed at from an editor for autocomplete. It just can't
  run anything that uses pandas.

`python -m streamlit` rather than `streamlit` helps where a policy blocks `streamlit.exe` itself, but not
when Smart App Control is blocking pandas.

**A desktop shortcut.** In PowerShell, from the project folder:

```powershell
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut("$Home\Desktop\Bank Enrichment.lnk")
$s.TargetPath = "$PWD\launch_dashboard.bat"
$s.WorkingDirectory = "$PWD"
$s.WindowStyle = 1
$s.Save()
```

Right-click the shortcut → Properties → Change Icon to give it an icon.

## An uptime monitor

`https://your-name.duckdns.org/health` answers `{"ok": true}` while the server is up and can reach the
database, and 503 when it can't. It needs no login and shows nothing else, so a free monitor (UptimeRobot,
Better Stack and the like) can check it every few minutes and email you if the server goes down. The
server's own Telegram alerts can't tell you that, since they come from the server.

## Monzo merchant names

Monzo's access needs confirming again in the Monzo app every so often. When it lapses, the `merchant`
field on new payments goes empty. Classification still works from the description, but the Telegram
card's quick-pick buttons fall back to a general list instead of your history with that merchant. The
confidential client in [SETUP.md step 4](../../SETUP.md#4-connect-monzo) lets the server renew the access
itself. Monzo doesn't document whether renewing alone is always enough, or whether you sometimes need to
open the Monzo app too, so keep an eye on it.

## Automatic deploys

A GitHub Actions workflow (`.github/workflows/ci.yml`) has two jobs:

1. **`test`**, on every pull request and every push to `main`: installs the dependencies and runs the tests.
2. **`deploy`**, only on a push to `main` and only if `test` passed: logs into the server over SSH and runs
   `git pull && docker compose up -d --build && docker image prune -f`.

It needs this once:

**1. A key for GitHub Actions to log into the server.** On the server:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/gh_deploy -C "github-actions-deploy" -N ""
cat ~/.ssh/gh_deploy.pub >> ~/.ssh/authorized_keys
cat ~/.ssh/gh_deploy   # copy the private key output
```

**2. Three repository secrets**, at `https://github.com/your-username/bank-enrichment/settings/secrets/actions`:

| Secret | Value |
|---|---|
| `DEPLOY_HOST` | The server's hostname or IP |
| `DEPLOY_USER` | The SSH user (e.g. `ubuntu`) |
| `DEPLOY_SSH_KEY` | The private key from step 1 |

**3. The server can `git pull` without a prompt.** The deploy runs unattended, so the key the server uses
for GitHub can't have a passphrase. If yours does, make a separate read-only deploy key:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/repo_deploy_key -C "bank-enrichment-deploy" -N ""
cat ~/.ssh/repo_deploy_key.pub
```

Add the public key at `https://github.com/your-username/bank-enrichment/settings/keys` → **Add deploy key**
(leave "Allow write access" off), then point Git at it for GitHub:

```bash
cat >> ~/.ssh/config << 'EOF'
Host github.com
    IdentityFile ~/.ssh/repo_deploy_key
    IdentitiesOnly yes
EOF
```

Test it: `cd ~/bank-enrichment && git pull` should finish with no prompt.

**4. The deploy user can run Docker without `sudo`**, since an unattended session can't answer a password
prompt:

```bash
groups ubuntu                    # the list should include "docker"
sudo usermod -aG docker ubuntu   # if it doesn't, then log out and back in
```

Every merge to `main` then deploys. Watch it at `https://github.com/your-username/bank-enrichment/actions`.

**Schema changes** need nothing by hand: the server applies `sql/postgres/schema.sql` on restart, which
only adds tables, columns and views. It never drops or rewrites data. A change that needs to is a one-off
script, run deliberately, after a backup.

**A changed `Caddyfile`** isn't picked up by `docker compose up -d --build`, since Caddy only reads it at
start. Reload it:

```bash
sudo docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```
