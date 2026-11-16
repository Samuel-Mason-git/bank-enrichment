# Security

[← Back to the README](../README.md)

How the server and the dashboard are kept to you.

**Who can reach what.** The server is reachable only at its two webhooks, the Monzo login and
`/health`. The webhooks can't use a login — Telegram and Monzo have to be able to call them — so
each checks a shared secret instead: Telegram sends `TELEGRAM_WEBHOOK_SECRET` in a header, and
Monzo's registered webhook URL carries `MONZO_WEBHOOK_SECRET` (redacted from the request logs). On
top of that, the bot only acts on updates from your own `TELEGRAM_CHAT_ID`, so someone who finds it
can't use it. Monzo authorization starts from `/oauth/start`, behind the login in `DASHBOARD_USER`
and `DASHBOARD_PASSWORD`, and the callback only completes an authorization the server issued itself.
