# Failure alerts

[← Back to the README](../README.md)

Every Anthropic error the pipeline catches — most importantly **running out of credit** —
also sends you a Telegram alert, with the same bot, so it doesn't 
sit unseen in a log file while transactions quietly stop being classified. Each kind is 
sent at most once per run (a run that fails 40 calls sends one message, not 40): credit 
balance too low, API key rejected, other Claude API errors, and the judge holding 
transactions back. Alerts contain a kind, what to do about it, and counts — never 
transaction text, and never an error's own message, since a parse error quotes the 
model's output. An alert that can't be sent is dropped, and the log line stands.
