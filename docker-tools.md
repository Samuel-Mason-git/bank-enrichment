# Docker Tools

All commands should be run from `~/bank-enrichment`.

## Service

| Command | Description |
|---|---|
| `docker compose up -d` | Start the container in the background |
| `docker compose down` | Stop and remove the container |
| `docker compose restart` | Restart the container |
| `docker compose ps` | Show if the container is running |

## Logs

| Command | Description |
|---|---|
| `docker compose logs -f` | Live log stream |
| `docker compose logs --tail 50` | Last 50 log lines |

## Builds

| Command | Description |
|---|---|
| `docker compose up -d --build && docker image prune -f` | Rebuild, restart, and remove old images |
| `docker images` | List all images on the machine |
| `docker system prune` | Remove stopped containers and unused images |

## Database

| Command | Description |
|---|---|
| `ls data/` | Check the data folder exists and contains the DB and log |
| `rm -f data/bank_enrichment_server.db` | Wipe the database (stop container first) |
