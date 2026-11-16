# Security

[← Back to the README](../README.md)

How the server and the dashboard are kept to you.

**Who can reach what.** The server answers at its two webhooks (`/recieve_monzo/` and
`/recieve_telegram/`), the Monzo authorization pages (`/oauth/start` and `/oauth/callback`), and
`/health`. The webhooks can't use a login, since Telegram and Monzo have to be able to call them,
so each checks a shared secret instead: Telegram sends `TELEGRAM_WEBHOOK_SECRET` in the
`X-Telegram-Bot-Api-Secret-Token` header, and Monzo's registered webhook URL carries
`MONZO_WEBHOOK_SECRET` as a `token` parameter (redacted from the request logs). If a secret isn't
set, the server logs a warning at startup and that webhook accepts requests from anyone who knows
its URL, so set both. On top of that, the bot only acts on updates from your own `TELEGRAM_CHAT_ID`,
so someone who finds it can't use it. `/oauth/start` is behind the login in `DASHBOARD_USER` and
`DASHBOARD_PASSWORD`, and the callback only completes an authorization the server issued itself.
