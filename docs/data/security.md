# Security

[← Back to the README](../../README.md)

How the server and the dashboard are kept to you.

**Who can reach what.** The server answers at its two webhooks (`/receive_monzo/` and
`/receive_telegram/`), the Monzo authorization pages (`/oauth/start` and `/oauth/callback`), and
`/health`. The webhooks can't use a login, since Telegram and Monzo have to be able to call them,
so each checks a shared secret instead: Telegram sends `TELEGRAM_WEBHOOK_SECRET` in the
`X-Telegram-Bot-Api-Secret-Token` header, and Monzo's registered webhook URL carries
`MONZO_WEBHOOK_SECRET` as a `token` parameter (redacted from the request logs). The server won't
start unless both are set, since without one of them that webhook would accept requests from anyone
who knows its URL. On top of that, the bot only acts on updates from your own `TELEGRAM_CHAT_ID`,
so someone who finds it can't use it. `/oauth/start` is behind the login in `MONZO_LOGIN_USER` and
`MONZO_LOGIN_PASSWORD`, and the callback only completes an authorization the server issued itself.

## Where the secrets are kept

The keys and tokens live in `config/.env`, and nowhere else: not in the database, and not in the
settings that go into bronze. The setup command writes it, and keeps the old file beside it as a
timestamped copy. Both are private from the moment they exist: owner-only (mode 0600) on macOS and
Linux, and on Windows an access list that keeps out everyone but your user. The command refuses to
finish if that can't be set. `config/` should stay out of anything you share or commit (it's in
`.gitignore`).

