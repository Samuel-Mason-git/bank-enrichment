# Security policy

## Reporting a vulnerability

Please don't open a public issue for a security problem. Report it privately with
GitHub's **Report a vulnerability** button, under the **Security** tab of this repo.
You'll get a reply as soon as I can, and I'll keep you posted on a fix.

## What's in scope

Bank Enrichment handles bank transaction data, API keys and webhook secrets, so these matter most:

- Anything that lets someone without the webhook or Telegram secrets change data, read transactions or reach the dashboard.
- Credentials or personal data that end up in the code, the logs, or the database backups the app writes.
- Anything that lets the Ask agent read or write beyond the grants in `sql/postgres/schema.sql`.

## Supported versions

Only the latest commit on `main` is supported.

## Your own deployment

Keep `config/.env` out of version control, and set both webhook secrets before the server starts
(the server refuses to start without them). [docs/data/security.md](docs/data/security.md) lists who can reach what.
