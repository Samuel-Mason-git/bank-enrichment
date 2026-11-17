# Subscriptions

[← Back to the README](../../README.md)

Recurring payments are spotted automatically and listed on the dashboard's
[Subscriptions](../money/dashboard.md) page, with what each costs per month and per year.

- Matching is by merchant name *or* counterparty name, since many direct debits only populate the latter.
- A fast-cadence pattern (weekly or fortnightly) needs more supporting occurrences before it's suggested.
- A pattern whose last occurrence is stale relative to its cadence is ignored.
- A confirmed subscription auto-deactivates if no matching transaction has landed in the last 2 months.

## Alerts

Two things get a Telegram message, each once. Each can be switched off, and tuned, on the Settings
page's **Alerts** tab:

- **A new price.** When the newest payment to a subscription's merchant differs from the amount stored
  for it, you're told the new amount. By default that's at least 50p and 5%. Update the subscription on
  this page if it's the new price.
- **A missed payment.** When the last payment is older than the subscription's period plus a grace
  period, you're told. The grace is 7 days by default, for every frequency. A later payment ends that
  gap. A gap is reported only in the two weeks after it becomes one, so an old or cancelled subscription
  doesn't send a message when the feature is first switched on.

Subscriptions with no payment ever seen aren't checked.
